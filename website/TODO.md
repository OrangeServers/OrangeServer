# 官网后续待办

官网已随 `main` 由 `.github/workflows/deploy-site.yml` 发布。本文不是产品合同，
只记录未完成的站点工作。

## 已完成

- 官网已合入公开 `main`，仓库已公开。
- Pages 由 GitHub Actions 发布。

## 内容优化

- [ ] 恢复 stars 动态 badge：`website/.vitepress/theme/components/HeroExtras.vue`
      仍可能使用静态占位，确认后换回
      `img.shields.io/github/stars/OrangeServers/OrangeServer`
- [ ] **英文站截图替换**：`website/public/screens/` 6 张均为中文界面。产品切英文后
      重截一套，并核对 `ScreensGallery.vue` 与 `guide/ai-ops.md` 的英文 caption
- [ ] 指南页扩充：安全架构、FAQ、功能详情页（现仅 3 篇，深度内容靠外链回仓库 docs/）
- [ ] SEO 收尾：sitemap、og meta、自定义 404 页

## 产品 UI（另起分支）

- [ ] **Web 终端页面重设计**：参考官网 hero 的终端窗口风格（TermMock.vue）
