// =============================================================================
// OrangeServer Frontend 路由
// ti3-TS: 从 router/index.js 迁移, 加 RouteMeta 类型扩展 + NavigationGuard 类型
// =============================================================================
import {
  createRouter,
  createWebHistory,
  type RouteRecordRaw,
  type RouteMeta,
  type NavigationGuardNext,
  type RouteLocationNormalized,
  type Router,
} from 'vue-router'

// 扩展 vue-router RouteMeta 接口, 业务侧加 titleKey/noLayout 字段
// I18N: title 改为 i18n key（menu.* 命名空间），菜单/面包屑/document.title 三处复用
declare module 'vue-router' {
  interface RouteMeta {
    titleKey?: string
    noLayout?: boolean
  }
}

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { titleKey: 'menu.login', noLayout: true },
  },
  {
    path: '/register',
    name: 'Register',
    component: () => import('@/views/Register.vue'),
    meta: { titleKey: 'menu.register', noLayout: true },
  },
  // SETUP-WIZARD: 首次部署配置向导（后端 setup 模式时由守卫引导至此）
  {
    path: '/setup',
    name: 'Setup',
    component: () => import('@/views/Setup.vue'),
    meta: { titleKey: 'menu.setup', noLayout: true },
  },
  // 独立会话窗口：纯终端 + SFTP，无侧边栏顶栏
  {
    path: '/remote-session',
    name: 'RemoteSession',
    component: () => import('@/views/RemoteSession.vue'),
    meta: { titleKey: 'menu.remoteSession', noLayout: true },
  },
  {
    path: '/',
    component: () => import('@/views/Layout.vue'),
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', name: 'Dashboard', component: () => import('@/views/Dashboard.vue'), meta: { titleKey: 'menu.dashboard' } },
      { path: 'host-list', name: 'HostList', component: () => import('@/views/HostList.vue'), meta: { titleKey: 'menu.hostList' } },
      { path: 'group-list', name: 'GroupList', component: () => import('@/views/GroupList.vue'), meta: { titleKey: 'menu.groupList' } },
      { path: 'sys-user', name: 'SysUserList', component: () => import('@/views/SysUserList.vue'), meta: { titleKey: 'menu.sysUser' } },
      { path: 'user-list', name: 'UserList', component: () => import('@/views/UserList.vue'), meta: { titleKey: 'menu.userList' } },
      { path: 'user-group', name: 'UserGroupList', component: () => import('@/views/UserGroupList.vue'), meta: { titleKey: 'menu.userGroup' } },
      {
        path: 'batch-command',
        name: 'BatchCommand',
        component: () => import('@/views/BatchCommand.vue'),
        meta: { titleKey: 'menu.batchCommand' },
      },
      {
        path: 'batch-script',
        name: 'BatchScript',
        component: () => import('@/views/BatchScript.vue'),
        meta: { titleKey: 'menu.batchScript' },
      },
      {
        path: 'ai-agent',
        name: 'AIAgent',
        component: () => import('@/views/AIAgent.vue'),
        meta: { titleKey: 'menu.aiAgent' },
      },
      {
        path: 'ai-runs',
        name: 'AiRuns',
        component: () => import('@/views/AiRuns.vue'),
        meta: { titleKey: 'menu.aiRuns' },
      },
      {
        path: 'ai-runs/:runId',
        name: 'AiRunDetail',
        component: () => import('@/views/AiRunDetail.vue'),
        meta: { titleKey: 'menu.aiRuns' },
      },
      { path: 'authority', name: 'Authority', component: () => import('@/views/Authority.vue'), meta: { titleKey: 'menu.authority' } },
      { path: 'cron', name: 'Cron', component: () => import('@/views/Cron.vue'), meta: { titleKey: 'menu.cron' } },
      { path: 'file', name: 'FileTransfer', component: () => import('@/views/FileTransfer.vue'), meta: { titleKey: 'menu.fileTransfer' } },
      { path: 'settings', name: 'Settings', component: () => import('@/views/Settings.vue'), meta: { titleKey: 'menu.settings' } },
      { path: 'user-info', name: 'UserInfo', component: () => import('@/views/UserInfo.vue'), meta: { titleKey: 'menu.userInfo' } },
      { path: 'log-login', name: 'AuditUserLog', component: () => import('@/views/AuditUserLog.vue'), meta: { titleKey: 'menu.auditUserLog' } },
      { path: 'log-exec', name: 'AuditComLog', component: () => import('@/views/AuditComLog.vue'), meta: { titleKey: 'menu.auditComLog' } },
      { path: 'log-op', name: 'AuditCzLog', component: () => import('@/views/AuditCzLog.vue'), meta: { titleKey: 'menu.auditCzLog' } },
    ],
  },
]

const router: Router = createRouter({
  history: createWebHistory(),
  routes,
})

// 需要 admin 角色的路由
const adminRoutes: readonly string[] = ['/authority', '/settings', '/user-list', '/user-group', '/sys-user', '/batch-script', '/ai-runs']
// 需要 admin 或 audit 角色的路由
const auditRoutes: readonly string[] = ['/log-login', '/log-exec', '/log-op']
// 需要 admin 或 user 角色的运维路由
const operatorRoutes: readonly string[] = ['/ai-agent', '/batch-command']

// 守卫匹配：精确命中或子路径命中（/ai-runs/:runId 这类参数路由按路由族前缀匹配）
function matchesGuard(routes: readonly string[], path: string): boolean {
  return routes.some((route) => path === route || path.startsWith(`${route}/`))
}

// 全局前置守卫：未登录跳转 + 角色权限检查
router.beforeEach(async (to: RouteLocationNormalized, _from: RouteLocationNormalized, next: NavigationGuardNext) => {
  // I18N: titleKey → document.title；语言切换时 applyTitleKey 记录的 key 会被重译
  const { applyTitleKey } = await import('@/i18n')
  applyTitleKey(to.meta.titleKey)
  if (to.path === '/login' || to.path === '/register' || to.path === '/setup') {
    // /setup 自身会查询后端模式：normal 模式下立即跳回 /login，不暴露向导
    return next()
  }
  // 本机开发态视觉/交互验证入口。Vite 在生产构建时会静态移除此分支，
  // 且 dev-auth-mock 还会限制 hostname 只能是 localhost/loopback。
  if (import.meta.env.DEV) {
    const devUser = new URLSearchParams(window.location.search).get('dev_login')
    const devRole = devUser === 'admin' ? 'admin' : devUser === 'user' ? 'user' : ''
    if (devRole) {
      if (matchesGuard(adminRoutes, to.path) && devRole !== 'admin') return next('/dashboard')
      if (matchesGuard(auditRoutes, to.path) && devRole !== 'admin') return next('/dashboard')
      if (matchesGuard(operatorRoutes, to.path) && !['admin', 'user'].includes(devRole)) return next('/dashboard')
      return next()
    }
  }
  // 检查登录状态
  try {
    const { checkAuth } = await import('@/api')
    const res = await checkAuth()
    if ((res as unknown as { setup_required?: boolean }).setup_required) {
      return next('/setup')
    }
    if (res.code === 3) {
      // /remote-session 独立窗口：未登录也放行（仍能渲染 UI，API 调用时再 401 兜底）
      // 主窗口调用 window.open 时 cookie 自动带，新窗口能正常通过 checkAuth
      if (to.path === '/remote-session') return next()
      return next('/login')
    }
  } catch (e: unknown) {
    // P1-7: 区分未登录 (401) vs 网络错误
    const err = e as { response?: { status?: number; data?: { setup_required?: boolean } } }
    // SETUP-WIZARD: 后端处于配置向导模式（业务接口统一 503 + setup_required）
    if (err.response?.status === 503 && err.response.data?.setup_required) {
      return next('/setup')
    }
    if (to.path === '/remote-session') {
      // 未登录 → 走 /login 重新认证
      if (err.response?.status === 401) {
        return next('/login')
      }
      // 网络错误 / 后端挂 → 放行，由 API 调用时再 401 兜底
      // 这样后端临时不可用时不会反复重定向 /login
      return next()
    }
    // 主窗口遇到任何 checkAuth 错误都走 /login
    return next('/login')
  }
  // 角色权限检查
  try {
    const { loadUserRole } = await import('@/store')
    const role: string | null = await loadUserRole()
    // P0-8: role 为 null（加载失败）或 ''（未加载）都按未知处理
    // 未知角色禁止访问 admin/audit/operator 页 → 走 /login 重新认证
    if (!role) {
      if (matchesGuard(adminRoutes, to.path) || matchesGuard(auditRoutes, to.path) || matchesGuard(operatorRoutes, to.path)) {
        return next('/login')
      }
      return next()
    }
    if (matchesGuard(adminRoutes, to.path) && role !== 'admin') {
      return next('/dashboard')
    }
    if (matchesGuard(auditRoutes, to.path) && !['admin', 'audit'].includes(role)) {
      return next('/dashboard')
    }
    if (matchesGuard(operatorRoutes, to.path) && !['admin', 'user'].includes(role)) {
      return next('/dashboard')
    }
  } catch (e) {
    // 角色加载函数本身抛异常（极端情况）→ 未知处理
    if (matchesGuard(adminRoutes, to.path) || matchesGuard(auditRoutes, to.path) || matchesGuard(operatorRoutes, to.path)) {
      return next('/login')
    }
  }
  next()
})

export default router
