#include <Arduino.h>
#include <WiFi.h>

// egress-agent-esp32 固件 v3（线协议契约见 ../PROTOCOL.md，修改协议先改契约）
// 拓扑: MiFi 路由器开 AP 隔离，ESP32 只能主动外连适配器（反向隧道）。

// WiFi 配置：优先 YCHZH，故障时自动回退到 YCHZHT
struct WiFiNet { const char* ssid; const char* pass; };
static const WiFiNet WIFI_LIST[] = {
  {"YCHZH",  "22372222"},
  {"YCHZHT", "22372222"}
};
static const int WIFI_COUNT = 2;
static int wifiIndex = 0;
static uint32_t lastWifiScan = 0;

IPAddress CTRL_HOST(115, 29, 231, 25);
constexpr uint16_t CTRL_PORT = 9999;
// 控制通道共享密钥：HELLO 首行携带，适配器不匹配即断开（v4，防公网扫描者冒充注册）。
// 适配器侧同名环境变量 ESP32_CTRL_SECRET；为空时适配器退回免鉴权（v3 兼容）。
static const char* CTRL_SECRET = "esp32-p1-7f3a9c";

#define LED_PIN 8
#define MAX_TUNNELS 4
#define TUNNEL_STACK 8192
// 隧道空闲上限。**曾经是 20000（20 秒），这是一个把整条桶打死的常量**：
// pump() 只要 20 秒两个方向都没有字节就主动断开，而大模型的「思考」阶段
// 恰恰允许很久没有首字节——网关自己的首字节期限是 ZEN_TTFB_S=60，实测
// 池内上游 TTFB p95 就有 21.3s。于是经 ESP32 的每个慢请求都在第 20.000 秒
// 被自己人掐断，网关只看到 transport 错误，三连之后就把这个桶 cordon 掉。
// 2026-10-28 实测（ECS→适配器→ESP32→httpbingo，CONNECT 200 + TLS1.3 握手
// 完成之后）：delay/10 与 delay/25 双双在 20.000s / 20.001s 归零。
// 现取 180s：远高于网关 60s 的首字节/静默期限，正常的慢推理不会被误杀；
// 真正的死隧道由 pump 的 `a.connected() && b.connected()` 立刻收尾，不靠它。
#define TUNNEL_IDLE_MS 180000UL
#define HEARTBEAT_MS 20000UL
#define TUNNEL_BUF 1024

static WiFiClient gCtrl;
static SemaphoreHandle_t gTunnelSlots;   // 隧道并发额度
static SemaphoreHandle_t gCtrlWriteMtx;  // 控制通道写互斥
static volatile int gActiveTunnels = 0;
static volatile uint32_t gTunnelTotal = 0;

// ---------- 串口/状态 ----------
void logf(const char *fmt, ...) {
  char buf[192];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  Serial.println(buf);
}

// ---------- WiFi ----------
void tryConnect(int idx) {
  if (idx < 0 || idx >= WIFI_COUNT) return;
  wifiIndex = idx;
  WiFi.disconnect(true);
  delay(100);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);
  const char* ssid = WIFI_LIST[wifiIndex].ssid;
  const char* pass = WIFI_LIST[wifiIndex].pass;
  Serial.printf("[WiFi] 尝试连接 %s (优先序列 %d/%d)\n", ssid, wifiIndex+1, WIFI_COUNT);
  WiFi.begin(ssid, pass);
}

void connectWiFi() {
  tryConnect(0);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 30000) {
    delay(300);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf(" OK IP=%s RSSI=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
    return;
  }
  Serial.println(" 首选网络失败，尝试备选网络...");
  for (int i = 1; i < WIFI_COUNT && WiFi.status() != WL_CONNECTED; i++) {
    tryConnect(i);
    uint32_t t1 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t1 < 20000) {
      delay(300);
      Serial.print(".");
    }
  }
  if (WiFi.status() == WL_CONNECTED)
    Serial.printf(" OK(IP备用网络) IP=%s RSSI=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  else
    Serial.println(" 所有网络连接失败，进入自动重连循环");
}

void checkWifiFallback() {
  if (WiFi.status() == WL_CONNECTED) return;
  // 当前网络连不上，尝试轮换到备选网络
  Serial.printf("[WiFi] 当前网络断开，尝试 fallback...\n");
  for (int i = 0; i < WIFI_COUNT; i++) {
    if (i == wifiIndex) continue;
    tryConnect(i);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
      delay(300);
    }
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("[WiFi] FALLBACK 成功 -> %s IP=%s\n", WIFI_LIST[wifiIndex].ssid, WiFi.localIP().toString().c_str());
      return;
    }
  }
  // 都失败，回到首选重试
  tryConnect(0);
}

// 回到首选网络。
// 这套换 IP 的机制是「重启 MiFi → 设备必然掉线 → fallback 到备用 WiFi」，
// 而原来的 loop() 只在**断线时**才跑 fallback，于是换过一次 IP 之后设备就
// 永远赖在备用网上：那台网里的 192.168.1.1 是家里的 TP-Link，适配器的换 IP
// 流程会（正确地）拒绝动手，第二次换 IP 就再也做不成了。
// 这里每 PRIMARY_RETURN_MS 扫一次，只要首选网络重新可见就切回去。
#define PRIMARY_RETURN_MS 180000UL   // 3 分钟
void maybeReturnToPrimary() {
  static uint32_t lastCheck = 0;
  if (wifiIndex == 0) return;                       // 已经在首选网
  if (WiFi.status() != WL_CONNECTED) return;        // 断线由 checkWifiFallback 管
  if (millis() - lastCheck < PRIMARY_RETURN_MS) return;
  lastCheck = millis();
  int n = WiFi.scanNetworks();
  bool primarySeen = false;
  for (int i = 0; i < n; i++) {
    if (WiFi.SSID(i) == WIFI_LIST[0].ssid) { primarySeen = true; break; }
  }
  WiFi.scanDelete();
  if (!primarySeen) {
    Serial.println("[WiFi] 首选网络不可见，继续留在备用网");
    return;
  }
  Serial.printf("[WiFi] 首选网络 %s 已恢复，切回\n", WIFI_LIST[0].ssid);
  tryConnect(0);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300);
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] 已回到首选网 IP=%s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[WiFi] 切回失败，保持原状");
  }
}

// ---------- 控制通道写（多任务安全） ----------
void ctrlSend(const String &line) {
  if (!gCtrl.connected()) return;
  xSemaphoreTake(gCtrlWriteMtx, portMAX_DELAY);
  gCtrl.print(line);
  gCtrl.flush();
  xSemaphoreGive(gCtrlWriteMtx);
}

// ---------- 带超时读一行 ----------
bool readLine(WiFiClient &in, String &line, uint32_t timeoutMs) {
  line = "";
  uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    while (in.available()) {
      char ch = (char)in.read();
      if (ch == '\n') return true;
      if (ch != '\r') line += ch;
      if (line.length() > 512) return false;
    }
    if (!in.connected()) return false;
    delay(2);
  }
  return false;
}

// ---------- 双向泵 ----------
void pump(WiFiClient &a, WiFiClient &b, uint32_t idleMs) {
  uint8_t buf[TUNNEL_BUF];  // 必须是每任务独立, 不能 static（多隧道会互相踩踏）
  uint32_t lastData = millis();
  while (a.connected() && b.connected()) {
    bool act = false;
    int n;
    while ((n = a.available()) > 0) {
      int r = a.read(buf, n < TUNNEL_BUF ? n : TUNNEL_BUF);
      if (r > 0) { b.write(buf, r); act = true; }
    }
    while ((n = b.available()) > 0) {
      int r = b.read(buf, n < TUNNEL_BUF ? n : TUNNEL_BUF);
      if (r > 0) { a.write(buf, r); act = true; }
    }
    if (act) lastData = millis();
    if (millis() - lastData > idleMs) break;
    delay(1);
  }
}

// ---------- 隧道任务 ----------
struct TunnelJob {
  String id;
  String host;
  uint16_t port;
};

void tunnelTask(void *arg) {
  TunnelJob *job = (TunnelJob *)arg;
  gActiveTunnels++;

  WiFiClient data, remote;
  data.setNoDelay(true);
  remote.setNoDelay(true);

  // 先连目标，成功后再向控制端开数据隧道，避免控制端拿到 TREADY 却等不到流量
  if (!remote.connect(job->host.c_str(), job->port)) {
    logf("[tun %s] 目标 %s:%u 不可达", job->id.c_str(), job->host.c_str(), job->port);
    ctrlSend("ERR " + job->id + " target-connect\n");
    goto DONE;
  }
  if (!data.connect(CTRL_HOST, CTRL_PORT)) {
    logf("[tun %s] 数据通道连接失败", job->id.c_str());
    ctrlSend("ERR " + job->id + " data-connect\n");
    remote.stop();
    goto DONE;
  }
  // TREADY 必须走数据通道（控制通道只走 OK/ERR/HB）
  data.print("TREADY " + job->id + "\n");
  data.flush();

  logf("[tun %s] %s:%u 已连通, 透传中 (活动=%d)", job->id.c_str(),
       job->host.c_str(), job->port, gActiveTunnels);
  ctrlSend("OK " + job->id + "\n");
  pump(data, remote, TUNNEL_IDLE_MS);
  data.stop();
  remote.stop();
  logf("[tun %s] 结束, 堆=%u", job->id.c_str(), (unsigned)ESP.getFreeHeap());

DONE:
  delete job;
  gActiveTunnels--;
  xSemaphoreGive(gTunnelSlots);
  vTaskDelete(NULL);
}

void startTunnel(const String &id, const String &host, uint16_t port) {
  if (gTunnelSlots == nullptr) return;
  if (xSemaphoreTake(gTunnelSlots, pdMS_TO_TICKS(3000)) != pdTRUE) {
    logf("[tun %s] 并发已满(%d), 拒绝", id.c_str(), MAX_TUNNELS);
    ctrlSend("ERR " + id + " busy\n");
    return;
  }
  TunnelJob *job = new TunnelJob{id, host, port};
  BaseType_t ok = xTaskCreate(tunnelTask, "tunnel", TUNNEL_STACK, job, 2, nullptr);
  if (ok != pdPASS) {
    logf("[tun %s] 任务创建失败", id.c_str());
    delete job;
    xSemaphoreGive(gTunnelSlots);
    ctrlSend("ERR " + id + " task-create\n");
    return;
  }
  gTunnelTotal++;
}

// ---------- 状态上报 ----------
String statusLine() {
  char buf[256];
  // SSID 与网关必须报出来：这颗设备会在两个同名网段的 WiFi 之间漫游
  // （都发 192.168.1.x），192.168.1.1 可能是家里的 TP-Link，也可能是要
  // 重启换 IP 的 MiFi。分不清在哪个网，服务端就没法安全地决定能不能重启。
  snprintf(buf, sizeof(buf),
           "STAT uptime=%lus heap=%u rssi=%d ip=%s gw=%s ssid=%s tunnels=%d total=%lu",
           millis() / 1000UL, (unsigned)ESP.getFreeHeap(), WiFi.RSSI(),
           WiFi.localIP().toString().c_str(),
           WiFi.gatewayIP().toString().c_str(),
           WiFi.SSID().c_str(), gActiveTunnels,
           (unsigned long)gTunnelTotal);
  return String(buf);
}

// ---------- 控制会话 ----------
void runControlSession() {
  gCtrl.setNoDelay(true);
  // HELLO 里带上 ssid/gw：适配器只校验第 4 个字段（密钥），多出来的字段
  // 只会进日志，却在每次重连时白送一条"我在哪个网"的证据。
  gCtrl.printf("HELLO esp32_p1 %s %s ssid=%s gw=%s\n",
               WiFi.localIP().toString().c_str(), CTRL_SECRET,
               WiFi.SSID().c_str(),
               WiFi.gatewayIP().toString().c_str());
  gCtrl.flush();
  logf("[ctrl] 已连上 %s, 等指令", CTRL_HOST.toString().c_str());

  uint32_t lastHb = millis();
  uint32_t lastRx = millis();

  while (gCtrl.connected() && WiFi.status() == WL_CONNECTED) {
    // 每 3 分钟自检一次"是不是该回首选网络了"。换 IP 依赖重启 MiFi，而重启
    // 之后设备一定先落到备用网；不自己走回来，第二次换 IP 就无从发起。
    maybeReturnToPrimary();
    String line;
    bool got = readLine(gCtrl, line, 1000);
    uint32_t now = millis();

    if (got) {
      lastRx = now;
      if (line.startsWith("T ")) {
        // "T <id> <host>:<port>"
        int s1 = line.indexOf(' ');
        int s2 = line.indexOf(' ', s1 + 1);
        if (s1 < 0 || s2 < 0) {
          ctrlSend("ERR bad-target\n");
        } else {
          String id = line.substring(s1 + 1, s2);
          String hp = line.substring(s2 + 1);
          int colon = hp.lastIndexOf(':');
          if (colon > 0) {
            uint32_t p = hp.substring(colon + 1).toInt();
            if (p > 0 && p < 65536)
              startTunnel(id, hp.substring(0, colon), (uint16_t)p);
            else
              ctrlSend("ERR " + id + " bad-port\n");
          } else {
            ctrlSend("ERR " + id + " bad-target\n");
          }
        }
      } else if (line == "PING") {
        ctrlSend("PONG\n");
      } else if (line == "STAT") {
        ctrlSend(statusLine() + "\n");
      } else if (line.startsWith("PONG")) {
        // 心跳应答, 无需处理
      } else if (line == "BYE") {
        logf("[ctrl] 控制端要求断开");
        break;
      }
    }

    // 心跳: 20s 无我方发包则发 HB
    if (now - lastHb > HEARTBEAT_MS) {
      lastHb = now;
      ctrlSend("HB\n");
    }
    // 90s 完全收不到控制端任何字节 => 链路已死
    if (now - lastRx > 90000UL) {
      logf("[ctrl] 90s 无回包, 主动断开重连");
      break;
    }
  }
  gCtrl.stop();
  logf("[ctrl] 断开, 堆=%u", (unsigned)ESP.getFreeHeap());
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  logf("\n=== esp32_p1 proxy bucket v4 (idle=180s) ===");
  logf("[boot] 堆=%u 芯片=%s rev=%d", (unsigned)ESP.getFreeHeap(),
       ESP.getChipModel(), ESP.getChipRevision());

  gTunnelSlots = xSemaphoreCreateCounting(MAX_TUNNELS, MAX_TUNNELS);
  gCtrlWriteMtx = xSemaphoreCreateMutex();
  connectWiFi();
}

void loop() {
  static uint32_t lastBlink = 0;
  static bool ledOn = false;
  static uint32_t lastFall = 0;

  // WiFi 掉线重连（首选失败时自动 fallback 到备选）
  if (WiFi.status() != WL_CONNECTED) {
    // 快闪 = 无 WiFi
    if (millis() - lastBlink > 120) { lastBlink = millis(); ledOn = !ledOn; digitalWrite(LED_PIN, ledOn); }
    if (millis() - lastFall > 8000) {
      lastFall = millis();
      checkWifiFallback();
    }
    delay(50);
    return;
  }

  // 连控制端（阻塞至会话结束）
  digitalWrite(LED_PIN, LOW);  // 有 WiFi 常亮
  logf("[ctrl] 连接 %s:%u ...", CTRL_HOST.toString().c_str(), CTRL_PORT);
  if (!gCtrl.connect(CTRL_HOST, CTRL_PORT)) {
    logf("[ctrl] 连接失败, 3s 后重试");
    delay(3000);
    return;
  }
  runControlSession();
  delay(2000);  // 重连退避
}
