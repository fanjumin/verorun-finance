# VeroRun CLI（`verorun`）

VeroRun AI 的**命令行客户端**。这是一个**薄客户端**：只调用 VeroRun 服务端现有 REST 端点
（`/admin/agent-matrix`、`/admin/automation`、`/admin/plugins` 等）与现有 JWT 鉴权，
**不实现任何 Agent 逻辑、不 import 核心模块、不注册为插件、不连接数据库**。

> 代码全部位于 `sdks/cli/`，与 `agent_matrix` / `auth-center` / `orchestrator` /
> `plugin_manager` 等核心模块零耦合。

当前版本：**与 VeroRun 系统主版本号保持一致**（复用仓库根 `VERSION` 文件，当前 `0.59.5`）。
CLI 作为系统的配套客户端，不再使用独立的 `0.2.0` 之类小版本号，避免与系统版本割裂、让用户误判过期。
实际版本号由 `verorun version` 读取，详见 `CHANGELOG.md`。

## 安装

```bash
cd sdks/cli
pip install -e .          # 或：pip install -r requirements.txt
```

安装后可用 `verorun` 命令；开发期也可用 `python -m verorun_cli`。

依赖（`requirements.txt`）：`click`、`requests`、`PyYAML`、`PyJWT`、`rich`。
（无 `python-dotenv`——本 CLI 不读取 `.env`，配置只读本机 `~/.verorun/config.yaml`。）

## 快速开始

```bash
# 1) 指向服务端（默认 http://localhost:8084；生产用环境变量或配置）
export VERORUN_BASE_URL=https://your.verorun.domain

# 2) 登录（管理员账号，所有 /admin/* 要求 is_admin）
#    推荐用 --password-stdin，避免明文密码出现在进程列表 / shell 历史
echo "$PWD_VAR" | verorun login -u admin --password-stdin

# 3) 对话
verorun chat "帮我写一份周报大纲"                 # Master 编排（同步）
verorun chat "解释一下 RAG" --agent 2 --stream   # 指定 Sub Agent 流式输出

# 4) 其它
verorun agents list
verorun tasks --recent
verorun sessions
verorun automation jobs --page 1 --limit 20
verorun plugins list --status enabled
verorun status
```

## 全局选项

| 选项 | 说明 |
|---|---|
| `--base-url URL` | 覆盖服务端入口（优先级高于配置与环境变量 `VERORUN_BASE_URL`） |
| `--insecure` | 关闭 SSL 校验（仅开发自签证书） |
| `--json` | 以 JSON 输出，便于脚本集成 |
| `--timeout SECONDS` | 单次请求超时秒数（默认 300，同步 /chat 可能较慢） |
| `--debug` | 出错时打印完整堆栈 |

## 命令树

```
verorun login | logout | whoami
verorun chat <message> [--session] [--agent <int>] [--stream/--no-stream] [--input <json>]
verorun dispatch <description> --agent <int> [--title] [--input <json>]
verorun agents list | get | toggle | test | capabilities <id>
verorun sessions
verorun session <sid> [--clear | --rm]
verorun search <keyword>
verorun tasks [--recent] [--limit N] | task <id> [--cancel|--retry|--logs]
verorun automation jobs [--page] [--limit] | job-run | job-toggle
verorun automation workflows [--page] [--limit] | workflow-run
verorun automation instances [--page] [--limit] | instance <id> [--pause|--resume|--cancel]
verorun plugins list [--status] | install | enable | disable | config <id> [--set <json>]
verorun status
verorun config get|set|list <key> [<value>]
verorun version
```

> 注：`sessions` / `agents` 的列表端点服务端**未实现分页参数**，故此处不提供
> `--page/--limit`（避免给出服务端会忽略的安慰剂选项）；`automation` 三类列表支持
> 分页，`tasks` 支持 `--limit`，`plugins list` 仅支持 `--status` 过滤。这些均对照
> 服务端 `routes.py` 逐一核对，未编造参数。

## 登录安全说明

* 优先用 `--password-stdin`（从 stdin 读密码），不要直接传 `-p/--password` 明文。
* `verorun login --api-key ek-...` 仅做本地格式校验后保存，**不经服务端验证**
  （仓库内无暴露的校验路由）；请用一次真实命令确认其有效。
* 登录态过期会在请求前本地自检（读取 JWT `exp`，含 30s 容差），提前提示而非等服务端 401。

## 凭证文件保护（本轮修复的重点）

登录后 token / apikey 写入 `~/.verorun/`（`token.json` / `credentials.json`）。
原实现仅调 `os.chmod(0o600)`，而该调用在 **Windows 上不映射到 NTFS ACL**
（实测 chmod 后 mode 仍为 `0o666`），等于管理员 JWT 毫无文件权限保护。

现改为按平台真正收紧：
* **POSIX**：用 `os.open(path, 0o600)` 原子创建，避免"先 open 再 chmod"之间的可读窗口。
* **Windows**：写入后调用 `icacls` 授予当前用户、断开 ACL 继承，使文件仅当前账户可读写；
  任何一步失败都会**明确告警**，绝不静默假装安全。

执行 `verorun status` 可查看凭证文件的真实权限保护状态。

## 配置

本机配置存于 `~/.verorun/`（`config.yaml` / `token.json` / `credentials.json`），
与服务器 `.env` 无关。

```bash
verorun config set server.base_url https://your.verorun.domain
verorun config get server.base_url
verorun config list
```

## 版权 / 版本保护说明

CLI 是独立分发的客户端，**不触发也不削弱**服务端 veroguard 完整性校验、发布签名、
版权水印等保护机制。详见主方案第 13–14 节（尤其：客户端严格遵守重定向 fail-closed，
3xx（含订阅/续费门禁的 302）一律按错误处理，绝不跟随以绕过门禁）。

## 开发

```bash
cd sdks/cli
pip install -e ".[test]"     # 含 pytest
pytest                        # 离线单测（tests/）
python -m compileall verorun_cli
```
