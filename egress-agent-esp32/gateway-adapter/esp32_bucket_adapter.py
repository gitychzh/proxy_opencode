#!/usr/bin/env python3
"""egress-agent-esp32 网关侧适配器（v4）。
把标准 egress 桶接口(CONNECT + /healthz :10005)翻译成 ESP32 反向隧道协议。
线协议契约见 ../PROTOCOL.md。每个入站连接(控制/数据)独立线程处理。
v4: 控制通道 HELLO 鉴权（ESP32_CTRL_SECRET，空=兼容 v3 免鉴权），防公网扫描者
冒充 ESP32 注册抢占桶槽（2026-10-27 ECS 9999 端口实测有 azenv/SSH 扫描流量）。
"""
import os, socket, threading, time, itertools, json, signal, select

CTRL_PORT = 9999
LISTEN_PORT = 10005
ADMIN_PORT = 10006
NAME = "esp32-bucket"
CTRL_SECRET = os.environ.get("ESP32_CTRL_SECRET", "").strip()
START = time.time()

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
pending = {}          # cid -> {"ready":Event,"tunnel":sock,"ok":bool,"leftover":bytes}
seq = itertools.count(1)
stats = {"tunnels": 0, "active": 0}
ctl_lock = threading.Lock()
ctl_replies = []      # 控制通道非 HB 应答

def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

def handle_conn(conn, addr, first):
    global esp, esp_peer, esp_peer_at
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
                               "egress_probe_ok": esp is not None, "tunnels_active": stats["active"],
                               "tunnels_total": stats["tunnels"], "uptime_s": int(time.time()-START)}).encode()
            client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                           + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
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
            log(f"[tun {cid}] 建链失败 {target}: ESP32 未在 20s 内回报 TREADY"
                f"（目标不可达/并发满/数据通道连不上）")
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
