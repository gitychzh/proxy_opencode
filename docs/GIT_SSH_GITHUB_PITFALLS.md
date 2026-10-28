# Git/SSH 推送踩坑总结

> 更新时间：2026-10-28  
> 适用范围：Windows + Git + SSH（GitHub）

## 症状
- `git push` 直接连 GitHub 报 `schannel: failed to receive handshake, SSL/TLS connection failed` 或 `TLS connect error: error:0A000126:SSL routines::unexpected eof while reading`
- 浏览器能正常访问 GitHub，但 git 命令走不通
- 切换代理（Clash Verge 127.0.0.1:7897）后依旧失败
- 本地有未推送提交，远程有新提交时 `pull --rebase` 也走 HTTPS 出错

## 根因
1. **全局 git 配置冲突**：存在 `url.https://github.com/.insteadof=ssh://git@github.com/`、`url.https://github.com/.insteadof=git@github.com:`、`url.git@github.com:.insteadof=https://github.com/` 等多条互相冲突的 `insteadOf` 规则，导致 git 在 HTTPS/SSH 之间来回重写 URL
2. **代理配置残留**：全局/本地曾设置 `http.proxy`、`https.proxy`，Clash 的 HTTP 代理与 Git 的 schannel/openssl 后端不兼容
3. **远程 URL 混用**：`origin` 同时配置 `fetch` 为 HTTPS、`push` 为 git@，再配合 `insteadOf` 更加混乱
4. **22 端口受限**：`github.com:22` 连通性不稳定，直接用 `git@github.com` 走 22 容易卡住

## 正确做法（已验证可用）
1. **清理代理和 SSL 后端配置**
   ```bash
   git config --global --unset http.proxy
   git config --global --unset https.proxy
   git config --local --unset http.proxy 2>/dev/null
   git config --local --unset https.proxy 2>/dev/null
   git config --global --unset http.sslBackend
   git config --global --unset https.sslBackend
   ```

2. **清理冲突的 URL 重写规则（仅在确认不需要时）**
   ```bash
   git config --global --unset-all url."https://github.com/".insteadOf
   git config --global --unset-all url."git@github.com:".insteadOf
   git config --global --unset-all url."ssh://git@github.com/".insteadOf
   ```

3. **用 SSH over 443（推荐）**
   在 `~/.ssh/config` 配置：
   ```ssh
   Host gh-ssh
       HostName ssh.github.com
       User git
       Port 443
       IdentityFile ~/.ssh/id_ed25519
       IdentitiesOnly yes
       UserKnownHostsFile ~/.ssh/known_hosts_gh443
       StrictHostKeyChecking accept-new
   ```
   
   切换远程并推送：
   ```bash
   git remote set-url origin gh-ssh:gitychzh/<repo>.git
   git remote set-url --push origin gh-ssh:gitychzh/<repo>.git
   git fetch origin
   git push origin main
   ```

4. **如果远程有新提交且本地要覆盖（极少用）**
   ```bash
   git fetch origin
   git reset --hard origin/main  # 谨慎：会丢弃本地未推送改动
   git push origin main --force  # 谨慎
   ```

## 注意事项
- 不要同时用 HTTP 代理和 git 的代理配置，浏览器代理不等于 git 能用
- SSH 密钥已在 GitHub 添加且 `ssh -T git@github.com` 返回认证成功才算通
- `gh-ssh` 用别名是为了避开全局 `insteadOf` 规则的干扰
- Windows 用 `connect.exe` 配合 socks5 也可行，但 `ssh.github.com:443` 更省事且更稳定
- 推送前先 `git ls-remote origin` 测试连通性
