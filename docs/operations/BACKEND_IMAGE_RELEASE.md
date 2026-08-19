# 发布流程（Release runbook）

本文是 OrangeServer 从「代码就绪」到「版本公开发布」的权威流程。发布者必须按顺序
执行每一节，在每节末尾确认对应验证门通过后再进入下一步。任何偏离（例如跳过全新
安装验证、直接改 main）都应在 Release 说明中显式记录原因。

核心约束（先记住这几条）：

- **双 registry 分离**：GHCR（国际）由 CI 推送，TCR（国内）由发布者本地 WSL 推送。
  境外 CI 无法推送到境内 TCR（实测 `PROTOCOL_ERROR` 空转超时），两者 digest 独立构建、
  **不强制一致**。
- **稳定 tag 与资产不可覆盖**：tag、GHCR 版本标签、Release 资产一旦存在就不删除、不
  移动；出问题改用新的补丁版本。
- **版本号不是单一来源**：散落在多处，发布时必须用 `ops/bump-release-version.sh` 统一
  刷新（见「版本号收敛」）。
- **隐私边界**：内网 IP、部署机账号、SSH 凭据绝不出现在仓库、文档、日志或提交中，
  示例一律用 `<deploy-host>` 这类占位符。

## 1. 发布前收口门

在目标 commit 的干净工作树执行以下门禁（`vX.Y.Z` 是待发布版本占位符，不要替换成已
发布过的 tag）：

```bash
git status --short --branch
git diff --check
pwsh -File ops/check-docs.ps1
bash ops/test-bootstrap-scripts.sh

cd backend
python -m pytest tests/ -q --ignore=app/tools/ansible_runner
cd ../frontend
npm ci --no-audit --no-fund
npm run build
cd ..
```

M1 自治还要用隔离的 MySQL、业务 Redis、自治 Redis Stack、Worker 和 SSH 测试资产做
smoke；从零安装必须能直接使用自治工作台并看到就绪状态，不要把「容器已启动」当成
自治闭环。入口：

```powershell
pwsh -File ops/smoke-ai-autonomy-s2.ps1 -ExpectedHead <40-hex-commit>
pwsh -File ops/smoke-ai-autonomy-s3.ps1 -ExpectedHead <40-hex-commit>
```

**全新安装的 setup 前状态必须专项验证**：未走 `/setup` 向导前，autonomy-worker 应
保持 `Up` 且日志为「等待配置就绪」，而不是 crash-loop。这是 v1.1.1 修复过的问题，
回归时最容易漏。

## 2. 版本号收敛

版本号硬编码在以下文件中，发布时必须全部刷新（`ops/bump-release-version.sh` 一键
完成）：

- `website/.vitepress/theme/installCommands.ts`（global + china 两处安装命令）
- `website/guide/deployment.md`
- `website/guide/getting-started.md`
- `website/zh/guide/deployment.md`
- `website/zh/guide/getting-started.md`
- `README.md`
- `README.zh-CN.md`
- `backend/Dockerfile`（`org.opencontainers.image.version` LABEL，不带 `v` 前缀）

```bash
bash ops/bump-release-version.sh <旧版本> <新版本>
# 例：bash ops/bump-release-version.sh v1.1.1 v1.2.0
```

注意：`v1.0.3` 这类「功能自某版本起提供」的历史标记不要动，脚本只替换当前稳定
版本号。刷新后跑一次 check-docs 确认无残留旧版本引用（`.playwright-mcp/`、`dist/`、
`node_modules/` 属于构建/测试产物，不在检查范围）。

## 3. 双 registry 发布

### 3.1 GHCR（国际，CI 推送）

`ghcr.io/orangeservers/orangeserver-backend` 由
`.github/workflows/publish-backend-image.yml` 从稳定 tag 构建并推送。该 workflow 校验
GitHub Release 存在（draft 或已发布均可）且 GHCR 同名 tag 不存在（不可覆盖）。

### 3.2 TCR（国内，本地 WSL 推送）

`ccr.ccs.tencentyun.com/xuwei777/orangeserver-backend` 由发布者本地 WSL 构建并推送。
境外 CI 无法推送到境内 TCR，因此不要依赖 CI 完成这一步：

```bash
wsl -d <wsl-发行版> -u root -e bash -c '
  rm -rf /root/ogs-build && mkdir -p /root/ogs-build
  cp -r /mnt/<源码路径>/backend /root/ogs-build/
  cd /root/ogs-build
  docker build -t ccr.ccs.tencentyun.com/xuwei777/orangeserver-backend:vX.Y.Z backend/
  docker push ccr.ccs.tencentyun.com/xuwei777/orangeserver-backend:vX.Y.Z
'
```

要点：

- 用 `wsl -u root`（root 有 docker socket 和 TCR 登录；普通用户可能在 docker 组之外）。
- 源码先 `cp` 进 Linux 文件系统再 build（`/mnt/...` 9p 挂载读得慢）。
- 两个 registry 是独立构建环境，digest 会不同，**这是预期，不是错误**。

### 3.3 发布后验证

两个 registry 都从未 `docker login` 的环境各做一次匿名拉取，确认平台为 `linux/amd64`、
镜像能启动到 healthy。国内链路再跑一次「从零安装验证」（见第 4 节）。

## 4. 发布步骤 checklist

假设代码已合并到 main 且收口门全绿，`vX.Y.Z` 为待发布版本：

1. **刷版本号**：`bash ops/bump-release-version.sh <旧> vX.Y.Z`，提交推 PR 合入 main。
2. **打 tag + Draft Release**：
   ```bash
   git switch main && git pull --ff-only origin main
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin vX.Y.Z
   # 大陆线路读 Gitee 同名 tag，需同步到 Gitee 镜像
   gh release create vX.Y.Z --draft --title "OrangeServer vX.Y.Z" --generate-notes
   ```
3. **CI 推 GHCR**：`gh workflow run "Publish backend image" --ref main -f tag=vX.Y.Z`，
   watch 到成功，确认 GHCR 匿名拉取成功。
4. **本地 WSL 推 TCR**（见 3.2），确认 TCR 匿名拉取成功。
5. **构建部署包并附加到 Release**：workflow 生成 `bootstrap-compose.sh`、
   `bootstrap-compose-cn.sh`、deploy tarball 和 sha256 并挂到 Draft Release。
6. **从零安装验证**（见下）——这一步不可跳过。
7. **发布**：`gh release edit vX.Y.Z --draft=false`。
8. **确认官网**：合入 main 后 GitHub Pages workflow 重新发布官网，核对安装命令已是
   新版本。

### 从零安装验证

在一次性主机（或独立 Compose project）用固定版本引导器，不要覆盖已有实例。

国内线路（大陆机器，走 Gitee + TCR + DaoCloud）：

```bash
set -o pipefail
curl -fsSL https://gitee.com/orangeservers/OrangeServer/raw/vX.Y.Z/ops/bootstrap-compose-cn.sh \
  | sudo bash -s -- --version vX.Y.Z
```

国际线路（境外机器，走 GitHub + GHCR）：

```bash
set -o pipefail
curl -fsSL https://github.com/OrangeServers/OrangeServer/releases/download/vX.Y.Z/bootstrap-compose.sh \
  | sudo bash -s -- --version vX.Y.Z
```

安装后验证：6 容器全部 `Up`，backend/mysql/redis/autonomy-redis healthy；setup 完成
前 worker 保持等待、不 crash-loop（见第 1 节）；`/local/health` 返回 200；完成
`/setup` 后登录、资产、审计、AI Provider、只读诊断正常。

## 5. 已知坑（踩过，别重蹈）

- **交接不可信**：接手发布时先跑收口门和合同测试验证「已提交/已通过」的说法，别
  直接往下走。历史上出现过「声称全完成但 6 文件未提交、PR 挂自带测试」。
- **CI 推不了 TCR**：境外 runner 连境内 registry 网络不通，TCR 只能本地 WSL 推。
- **升级路径 env 增补**：新增服务引入 `:?` 强校验的 env 键时，必须同步更新
  `docs/operations/UPGRADE.md`，否则旧实例升级 `docker compose up` 会硬失败。
- **合同测试与 CI 耦合**：`test_clean_deploy_contract.py` 断言 workflow 的守卫字符串，
  改 workflow 时同步改该测试，否则 CI 红。
- **全新安装 setup 前状态**：worker 不能 crash-loop，要等待配置就绪。

## 6. 隐私边界

- 内网 IP、部署机账号、SSH 私钥、数据库密码、API Key、Fernet/Flask secret **绝不**
  进入仓库、文档、日志、Release 说明或提交。示例一律用 `<deploy-host>` 占位。
- TCR 公开镜像地址（`ccr.ccs.tencentyun.com/xuwei777/...`）是 CN 安装的功能必需项，
  公开仓库可见、只能匿名拉取不能推送，允许出现在安装脚本和文档中；推送凭据只放
  本地或 CI secrets。

## 发布失败恢复

workflow checkout 输入的稳定 tag。若 GHCR 已成功而资产上传中断，重跑 workflow（原
tag）即可，不要删除 GHCR 镜像或移动 tag；若 tag 已存在且内容有误，改用新补丁版本，
绝不覆盖。
