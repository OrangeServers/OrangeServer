<template>
  <el-container style="height:100vh">
    <!-- 侧边栏 -->
    <el-aside :width="collapsed ? '68px' : '232px'" class="layout-sidebar" style="transition:width 0.25s cubic-bezier(0.4,0,0.2,1);overflow:hidden">
      <!-- Logo -->
      <div class="sidebar-logo">
        <img src="/juzi11.png" alt="logo" />
        <div v-show="!collapsed" class="logo-text">
          <span class="logo-name">OrangeServer</span>
          <span class="logo-sub">Mission Control</span>
        </div>
      </div>

      <!-- 菜单 -->
      <div class="sidebar-scroll">
        <el-menu
          :default-active="$route.path"
          :collapse="collapsed"
          :collapse-transition="false"
          background-color="transparent"
          text-color="rgba(255,255,255,0.65)"
          active-text-color="#FB923C"
          style="border:none"
        >
          <!-- 概览 -->
          <div v-show="!collapsed" class="sidebar-section">{{ $t('menu.group.overview') }}</div>
          <el-menu-item index="/dashboard" @click="$router.push('/dashboard')">
            <el-icon><DataBoard /></el-icon>
            <span>{{ $t('menu.dashboard') }}</span>
          </el-menu-item>

          <!-- 资产 -->
          <div v-show="!collapsed" class="sidebar-section">{{ $t('menu.group.assets') }}</div>
          <el-sub-menu index="asset">
            <template #title>
              <el-icon><Box /></el-icon><span>{{ $t('menu.group.assetManage') }}</span>
            </template>
            <el-menu-item index="/host-list" @click="$router.push('/host-list')">{{ $t('menu.hostList') }}</el-menu-item>
            <el-menu-item index="/group-list" @click="$router.push('/group-list')">{{ $t('menu.groupList') }}</el-menu-item>
            <el-menu-item index="/sys-user" v-if="isAdmin" @click="$router.push('/sys-user')">{{ $t('menu.sysUser') }}</el-menu-item>
          </el-sub-menu>

          <!-- 用户 -->
          <div v-show="!collapsed && isAdmin" class="sidebar-section">{{ $t('menu.group.users') }}</div>
          <el-sub-menu index="user" v-if="isAdmin">
            <template #title>
              <el-icon><User /></el-icon><span>{{ $t('menu.group.userManage') }}</span>
            </template>
            <el-menu-item index="/user-list" @click="$router.push('/user-list')">{{ $t('menu.userList') }}</el-menu-item>
            <el-menu-item index="/user-group" @click="$router.push('/user-group')">{{ $t('menu.userGroup') }}</el-menu-item>
          </el-sub-menu>

          <!-- 操作 -->
          <div v-show="!collapsed && (isAdmin || isUser)" class="sidebar-section">{{ $t('menu.group.operations') }}</div>
          <el-sub-menu index="ops" v-if="isAdmin || isUser">
            <template #title>
              <el-icon><Monitor /></el-icon><span>{{ $t('menu.group.opsCenter') }}</span>
            </template>
            <el-menu-item index="/batch-command" @click="$router.push('/batch-command')">{{ $t('menu.batchCommand') }}</el-menu-item>
            <el-menu-item v-if="isAdmin" index="/batch-script" @click="$router.push('/batch-script')">{{ $t('menu.batchScript') }}</el-menu-item>
            <el-menu-item
              index="open-remote-session"
              @click="openRemoteSession"
            >
              <el-icon><Promotion /></el-icon>
              <span>{{ $t('menu.webTerminal') }}</span>
            </el-menu-item>
          </el-sub-menu>

          <!-- AI 运维 -->
          <div v-show="!collapsed && (isAdmin || isUser)" class="sidebar-section">{{ $t('menu.group.intelligence') }}</div>
          <el-menu-item index="/ai-agent" v-if="isAdmin || isUser" @click="$router.push('/ai-agent')">
            <el-icon><Cpu /></el-icon><span>{{ $t('menu.aiAgent') }}</span>
          </el-menu-item>
          <!-- 自治任务：v1 后端 admin-only，菜单同样只对管理员可见 -->
          <el-menu-item index="/ai-runs" v-if="isAdmin" @click="$router.push('/ai-runs')">
            <el-icon><Stopwatch /></el-icon><span>{{ $t('menu.aiRuns') }}</span>
          </el-menu-item>
          <el-menu-item index="/ai-knowledge" v-if="isAdmin" @click="$router.push('/ai-knowledge')">
            <el-icon><Collection /></el-icon><span>{{ $t('menu.aiKnowledge') }}</span>
          </el-menu-item>

          <!-- 审计 -->
          <div v-show="!collapsed && (isAdmin || isAudit)" class="sidebar-section">{{ $t('menu.group.audit') }}</div>
          <el-sub-menu index="audit" v-if="isAdmin || isAudit">
            <template #title>
              <el-icon><Document /></el-icon><span>{{ $t('menu.group.logAudit') }}</span>
            </template>
            <el-menu-item index="/log-login" @click="$router.push('/log-login')">{{ $t('menu.auditUserLog') }}</el-menu-item>
            <el-menu-item index="/log-exec" @click="$router.push('/log-exec')">{{ $t('menu.auditComLog') }}</el-menu-item>
            <el-menu-item index="/log-op" @click="$router.push('/log-op')">{{ $t('menu.auditCzLog') }}</el-menu-item>
          </el-sub-menu>

          <!-- 系统 -->
          <div v-show="!collapsed" class="sidebar-section">{{ $t('menu.group.system') }}</div>
          <el-menu-item index="/authority" v-if="isAdmin" @click="$router.push('/authority')">
            <el-icon><Lock /></el-icon><span>{{ $t('menu.authority') }}</span>
          </el-menu-item>
          <el-menu-item index="/cron" v-if="isAdmin || isUser" @click="$router.push('/cron')">
            <el-icon><Timer /></el-icon><span>{{ $t('menu.cron') }}</span>
          </el-menu-item>
          <el-menu-item index="/file" v-if="isAdmin || isUser" @click="$router.push('/file')">
            <el-icon><Folder /></el-icon><span>{{ $t('menu.fileTransfer') }}</span>
          </el-menu-item>
          <el-menu-item index="/settings" v-if="isAdmin" @click="$router.push('/settings')">
            <el-icon><Setting /></el-icon><span>{{ $t('menu.settings') }}</span>
          </el-menu-item>
        </el-menu>
      </div>

      <!-- 底部状态卡（UI修复：接真实 /local/health，60s 轮询，替代硬编码"All systems normal"） -->
      <div v-show="!collapsed" class="sidebar-footer">
        <div class="sys-status">
          <span class="status-dot no-pulse" :class="healthStatus === 'ok' ? 'online' : healthStatus === 'fail' ? 'offline' : 'unknown'" style="width:6px;height:6px;margin-right:0"></span>
          <div style="flex:1;min-width:0">
            <div class="label">Service Status</div>
            <div class="value">{{ healthStatus === 'ok' ? 'All systems normal' : healthStatus === 'fail' ? 'Service unreachable' : 'Checking…' }}</div>
          </div>
        </div>
      </div>
    </el-aside>

    <el-container>
      <!-- 顶栏（毛玻璃） -->
      <el-header class="layout-header" height="64px">
        <div class="header-left">
          <el-tooltip :content="collapsed ? $t('layout.expandSidebar') : $t('layout.collapseSidebar')" placement="bottom">
            <span class="collapse-btn" @click="collapsed=!collapsed">
              <el-icon :size="18"><Fold v-if="!collapsed" /><Expand v-else /></el-icon>
            </span>
          </el-tooltip>
          <span class="header-divider"></span>
          <span class="breadcrumb-text">{{ $route.meta.titleKey ? $t($route.meta.titleKey) : 'OrangeServer' }}</span>
        </div>

        <div class="header-right">
          <el-tooltip :content="$t('layout.theme')" placement="bottom">
            <div class="header-icon-btn" @click="cycleTheme">
              <el-icon :size="18">
                <MagicStick v-if="store.theme.current === 'orange'" />
                <Sunny v-else-if="store.theme.current === 'black'" />
                <Moon v-else />
              </el-icon>
            </div>
          </el-tooltip>

          <!-- UI修复：移除通知铃铛——仅 tooltip+假红点、无任何点击响应（死交互+假信号）。
               通知中心落地前不保留死入口。 -->

          <el-dropdown trigger="click">
            <div class="header-avatar">
              <el-avatar :size="30" :src="store.user.avatar" />
              <span class="username">{{ store.user.alias || $t('layout.userFallback') }}</span>
              <el-icon :size="12"><ArrowDown /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="$router.push('/user-info')">
                  <el-icon><User /></el-icon>{{ $t('layout.userInfo') }}
                </el-dropdown-item>
                <el-dropdown-item divided @click="doLogout">
                  <el-icon><SwitchButton /></el-icon>{{ $t('layout.logout') }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <!-- 内容区 -->
      <el-main
        :class="{ 'agent-main': $route.path === '/ai-agent' }"
        style="background:var(--ogs-bg);padding:0;overflow-y:auto"
      >
        <!-- WHITESCREEN-FIX: 不能用 <transition mode="out-in"> 包 router-view——
             快速连续导航会打断 out-in 的 leave/enter 交接, enter 被丢弃后
             router-view 永久只剩 <!---- > 占位, 整个内容区白屏且无任何报错,
             只能刷新自救。改为纯 CSS 挂载动画 (styles/index.css 的 ogs-page-enter),
             视觉等效且不经过 Vue 过渡状态机。 -->
        <div class="page-container" :class="{ 'agent-page-container': $route.path === '/ai-agent' }">
          <router-view />
        </div>
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  Fold, Expand, ArrowDown, User, SwitchButton,
  MagicStick, Sunny, Moon, Promotion, Cpu, Stopwatch, Collection,
} from '@element-plus/icons-vue'
import { store, loadUserInfo, loadUserRole, loadSettings, applyTheme, clearAuthState } from '@/store'
import { t } from '@/i18n'
import { logout, getHealth } from '@/api'

/** 主题轮换顺序 (与 cycleTheme 一致) */
const THEME_ORDER: readonly string[] = ['blue', 'orange', 'black']

const router = useRouter()
const collapsed = ref<boolean>(window.innerWidth <= 1366)

// P3: 响应式自动折叠侧边栏
function _onResize(): void {
  collapsed.value = window.innerWidth <= 1366
}
window.addEventListener('resize', _onResize)
const isAdmin = computed<boolean>(() => store.user.role === 'admin')
const isAudit = computed<boolean>(() => store.user.role === 'audit')
const isUser = computed<boolean>(() => store.user.role === 'user')

async function doLogout(): Promise<void> {
  // REVIEW-14 P1-4: 先清本地状态（关 ws + 清 store），再调后端 logout
  //   避免出现 “后端已登出，前端残留旧头像 / 旧 tab” 的体验问题
  clearAuthState()
  try { await logout() } catch (_) { /* 后端失败不阻止前端跳转 */ }
  ElMessage.success(t('layout.logoutSuccess'))
  router.push('/login')
}

function openRemoteSession(e?: MouseEvent): void {
  // 保险：阻止可能的事件冒泡（el-menu 的 router 跳转已在模板中通过 index 去掉避免）
  if (e && e.stopPropagation) e.stopPropagation()
  const win = window.open('/remote-session', '_blank')
  if (!win) ElMessage.warning(t('layout.popupBlocked'))
}

function cycleTheme(): void {
  const idx: number = THEME_ORDER.indexOf(String(store.theme.current))
  const safeIdx: number = idx >= 0 ? idx : 0
  const next: string = THEME_ORDER[(safeIdx + 1) % THEME_ORDER.length] as string
  applyTheme(next)
}

// ---------- UI修复：真实健康状态（/local/health，60s 轮询） ----------
type HealthStatus = 'checking' | 'ok' | 'fail'
const healthStatus = ref<HealthStatus>('checking')
let healthTimer: ReturnType<typeof setInterval> | null = null

async function checkHealth(): Promise<void> {
  try {
    const res = (await getHealth()) as unknown as { status?: string }
    healthStatus.value = res.status === 'ok' ? 'ok' : 'fail'
  } catch {
    healthStatus.value = 'fail'
  }
}

onMounted(async () => {
  // 健康检查不依赖用户信息：同步启动，避免 await 窗口期组件卸载导致定时器泄漏
  checkHealth()
  healthTimer = setInterval(checkHealth, 60000)
  await loadUserInfo()
  await loadUserRole()
  await loadSettings()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', _onResize)
  if (healthTimer) {
    clearInterval(healthTimer)
    healthTimer = null
  }
})
</script>

<style scoped>
.layout-sidebar::-webkit-scrollbar { display: none; }
.el-menu-item.is-active {
  background-color: var(--ogs-sidebar-active) !important;
}
.agent-main {
  overflow: hidden !important;
}
.agent-page-container {
  height: 100%;
  box-sizing: border-box;
  overflow: hidden;
  /* AI 工作台吃满宽度：对话列自身限宽保证可读性，右栏贴边随时可瞥 */
  max-width: none;
}
</style>
