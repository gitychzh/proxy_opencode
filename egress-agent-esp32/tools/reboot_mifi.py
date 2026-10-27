#!/usr/bin/env python3
"""经 ESP32 桶隧道重启 192.168.1.1 MiFi(MF811) 更换公网IP（移动CGNAT重拨，不保证换IP）
用法: python3 tools/reboot_mifi.py（网关机上跑，走 gateway-adapter :10005 CONNECT）
流程: Digest 认证(admin/admin) -> POST xml_action.cgi?method=set&module=duster&file=reset
"""
import socket, hashlib, re, time, random

PROXY = ("127.0.0.1", 10005)
HOST = "192.168.1.1"
USER, PW = "admin", "admin"

def conn():
    s = socket.create_connection(PROXY, timeout=25)
    s.sendall(f"CONNECT {HOST}:80 HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    s.settimeout(25)
    resp = b""
    while b"\r\n\r\n" not in resp:
        d = s.recv(4096)
        if not d: break
        resp += d
    if b"200 Connection Established" not in resp:
        raise RuntimeError("CONNECT 失败: " + resp[:100].decode(errors="replace"))
    return s

def req(method, path, body=None, extra=""):
    s = conn()
    hdr = f"{method} {path} HTTP/1.1\r\nHost: {HOST}\r\n{extra}"
    if body is not None:
        hdr += f"Content-Length: {len(body)}\r\n"
    hdr += "Connection: close\r\n\r\n"
    s.sendall(hdr.encode() + (body.encode() if isinstance(body, str) else (body or b"")))
    data = b""
    try:
        while True:
            d = s.recv(65536)
            if not d: break
            data += d
    except socket.timeout: pass
    s.close()
    return data

def main():
    # 1 challenge
    d = req("GET", "/login.cgi").decode(errors="replace")
    m = re.search(r'Digest realm="([^"]+)", nonce="([^"]+)", qop="([^"]+)"', d)
    if not m: raise RuntimeError("拿不到 challenge: " + d[:200])
    realm, nonce, qop = m.groups()
    # 2 login (带 Digest 头, nc=00000001, uri=/cgi/xml_action.cgi)
    HA1 = hashlib.md5(f"{USER}:{realm}:{PW}".encode()).hexdigest()
    cnonce = hashlib.md5((str(random.randint(0, 99999)) + str(time.time())).encode()).hexdigest()[:16]
    HA2p = hashlib.md5("GET:/cgi/protected.cgi".encode()).hexdigest()
    resp1 = hashlib.md5(f"{HA1}:{nonce}:00000001:{cnonce}:{qop}:{HA2p}".encode()).hexdigest()
    HA2x = hashlib.md5("GET:/cgi/xml_action.cgi".encode()).hexdigest()
    resp_x1 = hashlib.md5(f"{HA1}:{nonce}:00000001:{cnonce}:{qop}:{HA2x}".encode()).hexdigest()
    h1 = (f'Digest username="{USER}", realm="{realm}", nonce="{nonce}", '
          f'uri="/cgi/xml_action.cgi", response="{resp_x1}", qop={qop}, '
          f'nc=00000001, cnonce="{cnonce}"')
    d2 = req("GET",
             f"/login.cgi?Action=Digest&username={USER}&realm={realm}&nonce={nonce}"
             f"&response={resp1}&qop={qop}&cnonce={cnonce}&temp=asr",
             extra=f"Authorization: {h1}\r\n")
    if b"WWW-Authenticate" in d2:
        raise RuntimeError("登录被拒: " + d2[:150].decode(errors="replace"))
    # 3 POST 重启 (nc=00000002)
    resp_x2 = hashlib.md5(f"{HA1}:{nonce}:00000002:{cnonce}:{qop}:{HA2x}".encode()).hexdigest()
    h2 = (f'Digest username="{USER}", realm="{realm}", nonce="{nonce}", '
          f'uri="/cgi/xml_action.cgi", response="{resp_x2}", qop={qop}, '
          f'nc=00000002, cnonce="{cnonce}"')
    xml = '<?xml version="1.0" encoding="US-ASCII"?><RGW><param><name>reboot</name><value>1</value></param></RGW>'
    d3 = req("POST", "/xml_action.cgi?method=set&module=duster&file=reset",
             body=xml, extra=f"Authorization: {h2}\r\nContent-Type: text/xml\r\n")
    line = d3.split(b"\r\n")[0].decode(errors="replace")
    print("reboot POST:", line)
    if "200" not in line:
        raise RuntimeError("POST 失败: " + d3[:200].decode(errors="replace"))
    print("重启指令已发, 等待设备重启(约60-90s)...")

if __name__ == "__main__":
    main()
