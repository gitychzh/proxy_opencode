#!/usr/bin/env python3
"""egress-agent-esp32 网关侧适配器（v4）。
把标准 egress 桶接口(CONNECT + /healthz :10005)翻译成 ESP32 反向隧道协议。
线协议契约见 ../PROTOCOL.md。每个入站连接(控制/数据)独立线程处理。
v4: 控制通道 HELLO 鉴权（ESP32_CTRL_SECRET，空=兼容 v3 免鉴权），防公网扫描者
冒充 ESP32 注册抢占桶槽（2026-10-27 ECS 9999 端口实测有 azenv/SSH 扫描流量）。
"""
import hashlib
import os, socket, threading, time, itertools, json, signal, select, re

CTRL_PORT = 9999
LISTEN_PORT = 10005
ADMIN_PORT = 10006
NAME = "esp32-bucket"
CTRL_SECRET = os.environ.get("ESP32_CTRL_SECRET", "").strip()
START = time.time()

# --- MiFi 换 IP（2026-10-29）------------------------------------------------
# 这颗 ESP32 挂在 4G MiFi 上，换 IP 的办法是重启那台 MiFi。MiFi 的管理页在
# 它自己的局域网里，从 ECS 只能经 ESP32 的反向隧道够到。但同一网段里可能
# 还有别的路由器（现场 192.168.1.1 有时是家里的 TP-Link TL-WR886N），所以
# **必须先验明正身再动手**：MiFi 的 /login.cgi 发 `Digest realm="Highwmg"`
# 挑战，TP-Link 则 302 到 tplogin.cn。认错设备就会把整个家庭网络重启掉。
MIFI_USER = os.environ.get("MIFI_ADMIN_USER", "admin").strip()
MIFI_PWD = os.environ.get("MIFI_ADMIN_PWD", "admin").strip()
MIFI_REALM = "Highwmg"
MIFI_REBOOT_PATH = "/xml_action.cgi?method=get&module=duster&file=reset"
MIFI_TIMEOUT_S = 25.0

# relay 的空闲上限。必须高于网关自己的容忍度（ZEN_TTFB_S=60 首字节、
# STREAM_STALL_S=60 流内静默），否则适配器会先于网关掐断一条本来健康的隧道。
RELAY_IDLE_S = 300

lock = threading.Lock()
esp = None            # 当前控制通道 socket
# ESP32 控制通道的源地址 == 它出网用的公网 IP（同一 NAT，与它发往上游的
# 源地址相同）。此前 /healthz 把它硬编码成 "1.1.1.1"：既污染审计账本，
# 也让「换 IP 后 cordon 自动解除」永远看不到变化。改由控制通道实测。
esp_peer = ""
esp_peer_at = ""
# HELLO 里由固件上报（2026-10-29 起）：设备会在两个同网段的 WiFi 之间漫游，
# 不报告 ssid/gw 就无法判断 192.168.1.1 到底是 MiFi 还是家里的路由器。
esp_ssid = ""
esp_gw = ""
pending = {}          # cid -> {"ready":Event,"tunnel":sock,"ok":bool,"leftover":bytes}
seq = itertools.count(1)
stats = {"tunnels": 0, "active": 0}
ctl_lock = threading.Lock()
ctl_replies = []      # 控制通道非 HB 应答
rotate_lock = threading.Lock()

def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

def handle_conn(conn, addr, first):
    global esp, esp_peer, esp_peer_at, esp_ssid, esp_gw
    if first.startswith(b"HELLO"):
        if CTRL_SECRET:
            # HELLO <name> <lan_ip> <secret>（v4）；不带 secret 的 HELLO 一律拒绝
            parts = first.split(b"\n", 1)[0].decode(errors="replace").split()
            if len(parts) < 4 or parts[3] != CTRL_SECRET:
                log(f"拒绝未鉴权 HELLO {addr} {first[:60]!r}")
                try:
                    conn.sendall(b"BYE\n")
                except Exception:
                    pass
                conn.close()
                return
        conn.settimeout(None)
        log(f"ESP32 上线 {first.decode(errors='replace').strip()} {addr}")
        esp_peer = str(addr[0])
        esp_peer_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # 固件自 v4 起在 HELLO 尾部附 `ssid=... gw=...`（v3 没有，保持兼容）
        hello = first.split(b"\n", 1)[0].decode(errors="replace").split()
        for tok in hello[4:]:
            if tok.startswith("ssid="):
                esp_ssid = tok[5:]
            elif tok.startswith("gw="):
                esp_gw = tok[3:]
        if esp_ssid or esp_gw:
            log(f"ESP32 网络 ssid={esp_ssid or '?'} gw={esp_gw or '?'}")
        with lock:
            if esp:
                try: esp.close()
                except: pass
            esp = conn
        buf = b""
        try:
            while True:
                d = conn.recv(4096)
                if not d: break
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.decode(errors="replace").strip()
                    if line == "HB":
                        try: conn.sendall(b"PONG\n")
                        except: break
                    else:
                        # ESP32 对建链失败回的是 `ERR <id> <reason>`
                        # （target-connect / data-connect / busy / bad-port…）。
                        # 以前它只被塞进 ctl_replies，从不与 pending 关联，于是
                        # 一次「目标不可达」也要白等 20s，日志还只能写"未在 20s
                        # 内回报 TREADY"——把已知原因说成未知。现在立刻结束等待。
                        if line.startswith("ERR "):
                            parts = line.split()
                            if len(parts) >= 2:
                                with lock:
                                    pe = pending.get(parts[1])
                                    if pe is not None:
                                        pe["err"] = (
                                            parts[2] if len(parts) > 2 else "unspecified"
                                        )
                                if pe is not None:
                                    pe["ready"].set()
                        with ctl_lock:
                            ctl_replies.append(line)
                            if len(ctl_replies) > 50:
                                ctl_replies.pop(0)
        except Exception as e:
            log("控制通道异常", e)
        log("ESP32 离线")
        with lock:
            if esp is conn: esp = None
        try: conn.close()
        except: pass
    elif first.startswith(b"TREADY"):
        # 数据通道: 剩余首行之后可能还有数据
        rest = first.split(b"\n", 1)
        head = rest[0].decode(errors="replace").strip()
        try:
            cid = head.split()[1]
        except IndexError:
            conn.close(); return
        leftover = rest[1] if len(rest) > 1 else b""
        with lock:
            p = pending.get(cid)
            if p:
                p["tunnel"] = conn
                p["leftover"] = leftover
                p["ok"] = True
            else:
                p = None
        if p:
            p["ready"].set()
        else:
            log("未知隧道", cid)
            conn.close()
    else:
        log("未知连接", addr, first[:40])
        conn.close()

def acceptor():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", CTRL_PORT))
    s.listen(16)
    log(f"ESP32 通道监听 0.0.0.0:{CTRL_PORT}")
    while True:
        conn, addr = s.accept()
        conn.settimeout(5)
        try:
            first = conn.recv(4096)
        except Exception:
            first = b""
        if not first:
            conn.close(); continue
        threading.Thread(target=handle_conn, args=(conn, addr, first), daemon=True).start()

def relay(c, t, cid=""):
    """双向透传一条已建立的隧道。

    以前这里是 `except Exception: pass`：隧道怎么死的、死在谁手里、传了多少
    字节，一条记录都没有。2026-10-28 排查「CONNECT 200 + TLS 握手成功，然后
    整 20.000 秒后连接消失」就只能靠 curl -v 反推。现在三个结局各留一行。
    """
    started = time.time()
    up = down = 0
    why = ""
    try:
        while True:
            r,_,_ = select.select([c,t], [], [], RELAY_IDLE_S)
            if not r:
                why = f"idle>{RELAY_IDLE_S}s"
                break
            for s in r:
                d = s.recv(65536)
                if not d:
                    why = "closed"
                    break
                if s is c:
                    up += len(d)
                else:
                    down += len(d)
                (t if s is c else c).sendall(d)
            if why:
                break
    except Exception as e:
        why = f"{type(e).__name__}: {e}"
    finally:
        for s in (c,t):
            try: s.close()
            except: pass
        with lock:
            stats["active"] = max(0, stats["active"]-1)
        log(f"[tun {cid}] 隧道结束 {time.time()-started:.1f}s "
            f"up={up} down={down} ({why})")

def open_tunnel(host, port, timeout=MIFI_TIMEOUT_S):
    """Ask the ESP32 for a data tunnel to host:port and return the live socket."""

    with lock:
        if not esp:
            raise RuntimeError("esp32-offline")
        cid = str(next(seq))
        ev = threading.Event()
        pending[cid] = {"ready": ev, "tunnel": None, "ok": False,
                        "leftover": b"", "err": ""}
        stats["tunnels"] += 1
        stats["active"] += 1
        ch = esp
    try:
        ch.sendall(f"T {cid} {host}:{port}\n".encode())
    except Exception as exc:
        pending.pop(cid, None)
        with lock:
            stats["active"] = max(0, stats["active"] - 1)
        raise RuntimeError(f"control-send-failed: {exc}") from exc
    ev.wait(timeout)
    p = pending.pop(cid, None)
    if not p or not p.get("ok") or not p.get("tunnel"):
        with lock:
            stats["active"] = max(0, stats["active"] - 1)
        raise RuntimeError("tunnel-failed: %s" % ((p or {}).get("err") or "no-tready"))
    return p["tunnel"]


def http_over(sock, method, path, headers=None, timeout=15.0):
    """One HTTP/1.1 exchange over an already-open tunnel socket."""

    lines = [f"{method} {path} HTTP/1.1", "Host: 192.168.1.1", "Connection: close"]
    for k, v in (headers or {}).items():
        lines.append(f"{k}: {v}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    buf = b""
    sock.settimeout(timeout)
    try:
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        pass
    head, _, body = buf.partition(b"\r\n\r\n")
    return head.decode("utf-8", "replace"), body


def mifi_rotate():
    """Reboot the 4G MiFi so the carrier hands out a new public IP.

    Safety contract: the gateway's ONT path re-dials a *known* device; here the
    tunnel just reaches whatever sits on the LAN's gateway address, and the
    same address has been observed to be the home TP-Link. So identification
    comes first — no matching digest realm, no reboot.
    """

    gw = esp_gw or "192.168.1.1"
    if not rotate_lock.acquire(blocking=False):
        return {"ok": False, "detail": "rotation already in progress"}
    try:
        sock = open_tunnel(gw, 80)
        try:
            head, _ = http_over(sock, "GET", "/login.cgi")
        finally:
            sock.close()
        if MIFI_REALM not in head:
            first = head.split("\r\n")[0] if head else "(no response)"
            log(f"[rotate] 拒绝: {gw} 不是 MiFi（{first}）")
            return {
                "ok": False,
                "detail": f"gateway {gw} is not the MiFi (no realm={MIFI_REALM}): {first}",
            }
        realm = re.search(r'realm="([^"]*)"', head)
        nonce = re.search(r'nonce="([^"]*)"', head)
        if not (realm and nonce):
            return {"ok": False, "detail": "MiFi sent no digest nonce"}
        realm, nonce = realm.group(1), nonce.group(1)
        # 与 js/base/utils.js doLogin 完全一致：HA2 固定按 /cgi/protected.cgi 计算
        ha1 = hashlib.md5(f"{MIFI_USER}:{realm}:{MIFI_PWD}".encode()).hexdigest()
        ha2 = hashlib.md5(b"GET:/cgi/protected.cgi").hexdigest()
        cnonce = hashlib.md5(str(time.time()).encode()).hexdigest()[:16]
        resp = hashlib.md5(
            f"{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}".encode()
        ).hexdigest()
        auth = (
            f'Digest username="{MIFI_USER}", realm="{realm}", nonce="{nonce}", '
            f'uri="/cgi/protected.cgi", response="{resp}", qop=auth, '
            f'nc=00000001, cnonce="{cnonce}"'
        )
        login = (
            f"/login.cgi?Action=Digest&username={MIFI_USER}&realm={realm}"
            f"&nonce={nonce}&response={resp}&qop=auth&cnonce={cnonce}&temp=asr"
        )
        sock = open_tunnel(gw, 80)
        try:
            head, _ = http_over(sock, "GET", login, {"Authorization": auth})
        finally:
            sock.close()
        if "200" not in (head.split("\r\n")[0] if head else ""):
            return {"ok": False, "detail": "MiFi login failed: %s"
                    % (head.split("\r\n")[0] if head else "no response")}
        # 这条 GET 在本机型上就是"重启"动作（2026-10-29 实测：发出后 MiFi 掉线
        # 并重新拨号，SSID/配置不变，不是恢复出厂）。它是 set 语义却写成 method=get，
        # 所以只能靠验明正身来防止误伤别的路由器。
        sock = open_tunnel(gw, 80)
        try:
            head, _ = http_over(sock, "GET", MIFI_REBOOT_PATH, {"Authorization": auth})
        except Exception:
            # 路由器在回包前就把服务停了 —— 对重启而言这正是成功的样子
            head = ""
        finally:
            try:
                sock.close()
            except Exception:
                pass
        status = head.split("\r\n")[0] if head else "(connection dropped)"
        log(f"[rotate] 已向 {gw} 发出重启（ssid={esp_ssid}）：{status}")
        return {
            "ok": True,
            "detail": f"MiFi reboot triggered on {gw} (ssid={esp_ssid or '?'}): {status}",
        }
    except Exception as exc:  # noqa: BLE001
        log(f"[rotate] 失败: {exc}")
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        rotate_lock.release()


def http_handler(client):
    client.settimeout(20)
    try:
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = client.recv(4096)
            if not d: return
            buf += d
            if len(buf) > 65536: return
        head, rest = buf.split(b"\r\n\r\n", 1)
        first = head.split(b"\r\n")[0].decode(errors="replace")
        try:
            method, target, ver = first.split(" ", 2)
        except ValueError:
            return
        if method == "GET" and target.rstrip("/") in ("/healthz", "/health"):
            body = json.dumps({"egress": NAME, "ready": esp is not None,
                               "egress_ip": esp_peer,
                               "egress_ip_source": "control-peer",
                               "egress_ip_at": esp_peer_at,
                               "ssid": esp_ssid, "gateway": esp_gw,
                               "egress_probe_ok": esp is not None, "tunnels_active": stats["active"],
                               "tunnels_total": stats["tunnels"], "uptime_s": int(time.time()-START)}).encode()
            client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                           + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            client.close()
            return
        if method == "POST" and target.rstrip("/") == "/rotate":
            # 标准 egress 桶接口：网关的 EgressRotator POST 这里，收到 ok 后
            # 自己去轮询 /healthz 等出口 IP 变化（ROTATE_TIMEOUT_S=420）。
            result = mifi_rotate()
            payload = json.dumps(result, ensure_ascii=False).encode()
            client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                           b"Content-Length: " + str(len(payload)).encode()
                           + b"\r\nConnection: close\r\n\r\n" + payload)
            client.close()
            return
        if method != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\nCONNECT only\r\n")
            client.close()
            return
        hostp = target
        if ":" in hostp:
            h, p = hostp.rsplit(":", 1)
            port = int(p)
        else:
            h, port = hostp, 443
        with lock:
            if not esp:
                log(f"CONNECT {target} 拒绝: ESP32 离线")
                client.sendall(b"HTTP/1.1 503 ESP32 Offline\r\nConnection: close\r\n\r\n")
                client.close()
                return
            cid = str(next(seq))
            ev = threading.Event()
            pending[cid] = {"ready": ev, "tunnel": None, "ok": False, "leftover": b""}
            stats["tunnels"] += 1
            stats["active"] += 1
        try:
            esp.sendall(f"T {cid} {h}:{port}\n".encode())
        except Exception as e:
            log(f"[tun {cid}] 控制通道下发失败 {target}: {e}")
            pending.pop(cid, None)
            stats["active"] -= 1
            client.sendall(b"HTTP/1.1 502\r\nConnection: close\r\n\r\n")
            client.close()
            return
        ev.wait(20)
        p = pending.pop(cid, None)
        if not p or not p.get("ok") or not p.get("tunnel"):
            err = (p or {}).get("err")
            detail = (
                f"ESP32 建链失败: {err}" if err
                else "ESP32 未在 20s 内回报 TREADY（目标不可达/并发满/数据通道连不上）"
            )
            log(f"[tun {cid}] {target}: {detail}")
            client.sendall(b"HTTP/1.1 502 Tunnel Failed\r\nConnection: close\r\n\r\n")
            client.close()
            with lock: stats["active"] = max(0, stats["active"]-1)
            return
        t = p["tunnel"]
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if rest: t.sendall(rest)
        if p.get("leftover"): client.sendall(p["leftover"])
        threading.Thread(target=relay, args=(client, t, cid), daemon=True).start()
    except Exception as e:
        log("http err", e)
        try: client.close()
        except: pass

def http_acceptor():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", LISTEN_PORT))
    s.listen(128)
    log(f"zen-gw 桶接口监听 0.0.0.0:{LISTEN_PORT}")
    while True:
        c, a = s.accept()
        threading.Thread(target=http_handler, args=(c,), daemon=True).start()

def send_ctrl_and_wait(line, prefixes, timeout=10.0):
    """向 ESP32 控制通道发一条指令并等待前缀匹配的应答。返回 (reply|None, err|None)。"""
    with lock:
        s = esp
    if not s:
        return None, "esp32-offline"
    with ctl_lock:
        ctl_replies.clear()
    try:
        s.sendall((line + "\n").encode())
    except Exception as e:
        return None, f"send-fail:{e}"
    end = time.time() + timeout
    while time.time() < end:
        with ctl_lock:
            for r in ctl_replies:
                for p in prefixes:
                    if r.startswith(p):
                        return r, None
        time.sleep(0.2)
    return None, "timeout"

def admin_response(client, code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode()
    client.sendall(b"HTTP/1.1 " + str(code).encode() + b" OK\r\nContent-Type: application/json; charset=utf-8\r\n"
                   b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)

def admin_handler(client):
    """本地管理接口（127.0.0.1:10006）。
    GET  /status                        -> 适配器+ESP32 状态
    POST /wifi  {"ssid":..,"pass":..}   -> SETWIFI（写入 NVS 并重启）
    GET  /wifi                          -> GETWIFI
    DELETE /wifi                        -> CLEARWIFI
    """
    client.settimeout(25)
    try:
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = client.recv(4096)
            if not d: return
            buf += d
            if len(buf) > 65536: return
        head, rest = buf.split(b"\r\n\r\n", 1)
        lines = head.split(b"\r\n")
        try:
            method, target, _ = lines[0].decode(errors="replace").split(" ", 2)
        except ValueError:
            return
        path = target.split("?")[0].rstrip("/") or "/"
        with lock:
            online = esp is not None
        if method == "GET" and path == "/status":
            admin_response(client, 200, {"online": online, "stats": stats,
                                         "uptime_s": int(time.time()-START)})
            return
        if not online:
            admin_response(client, 503, {"ok": False, "error": "esp32-offline"})
            return
        if method == "GET" and path == "/wifi":
            r, err = send_ctrl_and_wait("GETWIFI", ["WIFI "])
            if err:
                admin_response(client, 504, {"ok": False, "error": err})
            else:
                parts = r.split()
                admin_response(client, 200, {"ok": True, "ssid": parts[1] if len(parts) > 1 else "",
                                             "source": parts[2] if len(parts) > 2 else ""})
            return
        if method == "POST" and path == "/wifi":
            try:
                body = json.loads(rest.decode() or "{}")
                ssid = str(body.get("ssid", "")).strip()
                password = str(body.get("pass", ""))
            except Exception as e:
                admin_response(client, 400, {"ok": False, "error": f"bad-json:{e}"})
                return
            if not ssid:
                admin_response(client, 400, {"ok": False, "error": "ssid-required"})
                return
            r, err = send_ctrl_and_wait(f"SETWIFI {ssid} {password}", ["OK SETWIFI", "ERR SETWIFI"], timeout=8)
            if err:
                admin_response(client, 504, {"ok": False, "error": err})
            else:
                admin_response(client, 200, {"ok": r.startswith("OK"), "reply": r, "ssid": ssid})
            return
        if method == "DELETE" and path == "/wifi":
            r, err = send_ctrl_and_wait("CLEARWIFI", ["OK CLEARWIFI", "ERR CLEARWIFI"], timeout=8)
            if err:
                admin_response(client, 504, {"ok": False, "error": err})
            else:
                admin_response(client, 200, {"ok": r.startswith("OK"), "reply": r})
            return
        admin_response(client, 404, {"ok": False, "error": "not-found"})
    except Exception as e:
        log("admin err", e)
        try: client.close()
        except: pass

def admin_acceptor():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", ADMIN_PORT))
    except Exception as e:
        log(f"管理接口绑定失败 {e}")
        return
    s.listen(8)
    log(f"管理接口监听 127.0.0.1:{ADMIN_PORT}")
    while True:
        c, a = s.accept()
        threading.Thread(target=admin_handler, args=(c,), daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=acceptor, daemon=True).start()
    threading.Thread(target=http_acceptor, daemon=True).start()
    threading.Thread(target=admin_acceptor, daemon=True).start()
    log(f"esp32桶适配器v4启动 auth={'on' if CTRL_SECRET else 'off(兼容v3)'}")
    signal.pause()
