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
NAME = "esp32-bucket"
CTRL_SECRET = os.environ.get("ESP32_CTRL_SECRET", "").strip()
START = time.time()

lock = threading.Lock()
esp = None            # 当前控制通道 socket
pending = {}          # cid -> {"ready":Event,"tunnel":sock,"ok":bool,"leftover":bytes}
seq = itertools.count(1)
stats = {"tunnels": 0, "active": 0}

def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

def handle_conn(conn, addr, first):
    global esp
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

def relay(c, t):
    try:
        while True:
            r,_,_ = select.select([c,t], [], [], 120)
            if not r: break
            for s in r:
                d = s.recv(65536)
                if not d: raise Exception("closed")
                (t if s is c else c).sendall(d)
    except Exception:
        pass
    finally:
        for s in (c,t):
            try: s.close()
            except: pass
        with lock:
            stats["active"] = max(0, stats["active"]-1)

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
            body = json.dumps({"egress": NAME, "ready": esp is not None, "egress_ip": "1.1.1.1",
                               "egress_ip_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
        except Exception:
            pending.pop(cid, None)
            stats["active"] -= 1
            client.sendall(b"HTTP/1.1 502\r\nConnection: close\r\n\r\n")
            client.close()
            return
        ev.wait(20)
        p = pending.pop(cid, None)
        if not p or not p.get("ok") or not p.get("tunnel"):
            client.sendall(b"HTTP/1.1 502 Tunnel Failed\r\nConnection: close\r\n\r\n")
            client.close()
            with lock: stats["active"] = max(0, stats["active"]-1)
            return
        t = p["tunnel"]
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if rest: t.sendall(rest)
        if p.get("leftover"): client.sendall(p["leftover"])
        threading.Thread(target=relay, args=(client, t), daemon=True).start()
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

if __name__ == "__main__":
    threading.Thread(target=acceptor, daemon=True).start()
    threading.Thread(target=http_acceptor, daemon=True).start()
    log(f"esp32桶适配器v4启动 auth={'on' if CTRL_SECRET else 'off(兼容v3)'}")
    signal.pause()
