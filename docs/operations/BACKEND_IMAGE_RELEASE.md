# 后端容器镜像发布

正式版本的后端镜像由 `.github/workflows/publish-backend-image.yml` 从稳定标签
构建。`publish` job 仅在公开的规范仓库中运行；私有归档或 Fork 即使手动触发也
不会发布镜像。

## 发布前收口门

在创建稳定 tag 前，先在目标 commit 的干净工作树执行以下门禁。`vX.Y.Z` 只是待
发布版本占位符，不要把它替换成已经发布过的 tag；稳定 tag 和 Release 资产均不可
覆盖。

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

# 生成与正式工作流相同的版本化部署包，并在发布前校验包名和摘要。
rm -rf release-assets
bash ops/build-deploy-bundle.sh --version vX.Y.Z --output-dir release-assets
(
  cd release-assets
  sha256sum -c "orangeserver-deploy-vX.Y.Z.tar.gz.sha256"
)
tar -tzf "release-assets/orangeserver-deploy-vX.Y.Z.tar.gz" \
  | grep -E '^(orangeserver/)?(backend/mysqldir/orange.sql|frontend/dist/index.html|ops/bootstrap-compose.sh)$'
```

M1 自治还要单独使用隔离的 MySQL、业务 Redis 7、自治 Redis 8、Worker 和 SSH 测试
资产验证；标准发布栈只验证 feature flag 默认关闭，不把普通 Compose 启动误报为
自治闭环。源码 exact-HEAD smoke 的入口是：

```powershell
pwsh -File ops/smoke-ai-autonomy-s2.ps1 -ExpectedHead <40-hex-commit>
pwsh -File ops/smoke-ai-autonomy-s3.ps1 -ExpectedHead <40-hex-commit>
```

两个脚本都会从 Git archive 构造 disposable 栈，并在成功后清理专属容器、网络、
卷和临时镜像；执行前工作树必须干净。它们验证的是 M1 隔离执行/恢复和聊天草稿边界，
不替代真实 Provider 凭据下的人工 Run 验收。

## 首次公开发布

1. 确认仓库已经公开，并完成发布前的全量验收。
2. 创建并推送稳定 SemVer 标签，再为同一标签创建 **Draft Release**，例如
   `v1.0.0`，暂时不要发布。工作流会在构建镜像前验证 Draft Release 已存在。
3. 手动运行 `Publish backend image`，输入该 tag。工作流从 tag 重新构建前端、
   构建 `linux/amd64` 后端镜像，并推送
   `ghcr.io/orangeservers/orangeserver-backend:v1.0.0`。配置国内镜像发布后，同一次
   构建还会推送
   `ccr.ccs.tencentyun.com/xuwei777/orangeserver-backend:v1.0.0`，并验证两个
   registry 的 digest 完全一致。项目不发布 `latest`；如果 GHCR 已存在同名版本
   标签，工作流会在构建前拒绝覆盖。
4. 工作流把国际版和中国大陆版引导器（`bootstrap-compose.sh`、
   `bootstrap-compose-cn.sh`）以及带 SHA256 的 Compose 部署包附加到
   Draft Release。已存在的同名资产不会被覆盖；SHA256 用于发现下载损坏，发布者
   真实性仍依赖固定 tag、GitHub 仓库权限和 Release 管理权限。
5. 首次推送后，在 GitHub Packages 中把 package 设为 **Public**，从未登录 GHCR
   的机器验证镜像匿名拉取，并下载部署包复核 SHA256。
6. 上述验证全部完成后才把 Draft Release 发布，避免用户看到尚未就绪的下载入口。
7. 将部署 `.env` 中的 `OGS_BACKEND_IMAGE` 和 `OGS_BACKEND_TAG` 指向已验证的
   版本，然后执行 `make docker-up-image`。

## 发布命令

合并到 `main` 并完成收口门后，使用同一个稳定版本号完成 tag、Draft Release、镜像
和部署包发布。以下命令需要有仓库写权限；不会自动发布 Draft Release：

```bash
release_version=vX.Y.Z
git switch main
git pull --ff-only origin main
git tag -a "$release_version" -m "Release $release_version"
git push origin "$release_version"
# 大陆一键安装读取 Gitee 同名 tag；把同一 annotated tag 推到
# https://gitee.com/orangeservers/OrangeServer 后再发布该线路。
gh release create "$release_version" --draft --title "OrangeServer $release_version" \
  --generate-notes
gh workflow run "Publish backend image" --ref main \
  -f tag="$release_version"
gh run watch
gh release view "$release_version" --json isDraft,assets,tagName
```

确认 GHCR/TCR（若启用）镜像 digest、部署包 SHA256、Gitee 同名 tag、全新安装和
浏览器健康检查均通过后，再显式发布 Release。随后把 README 与官网中英
getting-started/deployment 里的安装版本钉从旧稳定版改到该 tag；未打 tag 前不要
改这些入口。合入 `main` 后确认 GitHub Pages 工作流已发布官网。

确认上述检查通过后，再显式发布 Release：

```bash
gh release edit "$release_version" --draft=false
```

如果 GHCR 已成功而 TCR 或 Release 附件阶段中断，使用工作流的恢复输入重新运行，
不要移动稳定 tag、删除 GHCR 镜像或覆盖同名资产：

```bash
gh workflow run "Publish backend image" --ref main \
  -f tag="$release_version" -f tcr_sync_only=true
```

### 从零安装验证

发布后在一次性主机或独立 Compose project 使用固定版本引导器；不要在已有实例上
用这条命令覆盖安装目录：

```bash
set -o pipefail
curl -fsSL \
  "https://github.com/OrangeServers/OrangeServer/releases/download/${release_version}/bootstrap-compose.sh" \
  | sudo bash -s -- \
      --version "$release_version" \
      --project-name orangeserver_release_check \
      --install-dir /opt/orangeserver-release-check \
      --port 18082
```

安装完成后，先确认 `/local/health` 返回 HTTP 200，再完成 `/setup`，检查登录、
资产、审计、AI Provider 和固定只读诊断。M1 自治的标准发布验证仍应保持
`OGS_AI_AUTONOMY_ENABLED` 为空；要验收自治，请回到隔离 smoke 或开发覆盖层。

## 启用腾讯云 TCR 同步发布

在规范 GitHub 仓库中配置以下 Actions Secrets：

- `TCR_USERNAME`：腾讯云账号 ID（UIN）。
- `TCR_PASSWORD`：TCR 个人版实例的访问密码。

确认目标仓库
`ccr.ccs.tencentyun.com/xuwei777/orangeserver-backend` 为公有仓库后，再设置
Actions Variable `TCR_ENABLED=true`。未设置该变量时，发布工作流只推送 GHCR；
因此 Fork 和尚未配置 TCR 凭据的仓库不会意外尝试国内发布。

每次发布后必须从未执行 `docker login` 的环境拉取 TCR 版本，并确认匿名拉取
成功、平台为 `linux/amd64`、digest 与 GHCR 相同。Secrets 只用于推送，不能写入
仓库、Release 附件、日志或部署文档。

不要在仓库仍私有时手工公开镜像：Python 镜像包含应用源码，公开镜像基本等同于
提前公开后端代码。

## 发布失败

工作流会 checkout 输入的稳定标签，而不是用默认分支内容冒充版本。如果失败发生在
上传资产之前，可以排除故障后重试；一旦 Draft Release 已包含任何同名资产，不要
覆盖或移动该标签，修复后改用新的补丁版本。已经发布的 Release 会被工作流拒绝。

如果 GHCR 版本标签已经成功发布，但腾讯云 TCR 同步或 Release 附件阶段中断，不要
删除 GHCR 镜像或移动稳定标签。重新运行 `Publish backend image`，输入原版本并启用
`tcr_sync_only`；工作流会复用现有 GHCR manifest，通过 registry-to-registry 复制
恢复 TCR，并继续生成 Release 附件，不重新构建或覆盖 GHCR。
