<template>
  <div>
    <!-- 页面标题 -->
    <div class="page-header">
      <div class="page-title">
        <div>
          <div class="page-eyebrow">Overview</div>
          <h2>{{ $t('menu.dashboard') }}</h2>
          <p class="page-subtitle">{{ $t('dashboard.subtitle') }}</p>
        </div>
      </div>
      <div class="page-actions">
        <el-tag size="default" effect="plain" round class="tag-border">
          <span class="status-dot online no-pulse" style="width:6px;height:6px;margin-right:6px"></span>
          {{ $t('dashboard.realtimeSync') }}
        </el-tag>
        <el-button :icon="Refresh" plain @click="refresh">{{ $t('dashboard.refreshData') }}</el-button>
      </div>
    </div>

    <!-- 统计卡片 -->
    <el-row :gutter="20" class="mb-24">
      <el-col :span="6" v-for="(item, i) in stats" :key="item.labelKey">
        <div
          class="stat-card"
          :style="{ '--accent': item.color }"
          :title="statTooltip(item)"
          @click="item.route && $router.push(item.route)"
        >
          <div class="stat-top">
            <div class="stat-icon" :style="{ background: item.color + '14', color: item.color }">
              <el-icon :size="20"><component :is="item.icon" /></el-icon>
            </div>
            <div class="stat-meta">
              <el-icon v-if="item.route" :size="14" class="stat-arrow" color="var(--ogs-text-muted)"><ArrowRight /></el-icon>
            </div>
          </div>
          <div class="stat-value num">{{ item.value }}</div>
          <div class="stat-label">{{ $t(item.labelKey) }}</div>
          <div class="stat-sub num">{{ item.sub }}</div>
          <div class="stat-accent"></div>
        </div>
      </el-col>
    </el-row>

    <!-- Row 2: 趋势 + 分布 -->
    <el-row :gutter="20" class="mb-24">
      <el-col :span="14">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-icon"><el-icon><TrendCharts /></el-icon></span>
            <span class="panel-title">{{ $t('dashboard.panel.loginTrend') }}</span>
            <span class="panel-sub">Last 30 days</span>
            <div class="panel-actions">
              <el-radio-group v-model="trendRange" size="small">
                <el-radio-button value="7d" label="7d" />
                <el-radio-button value="30d" label="30d" />
              </el-radio-group>
            </div>
          </div>
          <div class="panel-body chart-body">
            <v-chart :option="lineOption" class="chart-320" autoresize />
          </div>
        </div>
      </el-col>
      <el-col :span="10">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-icon"><el-icon><PieChart /></el-icon></span>
            <span class="panel-title">{{ $t('dashboard.panel.resourceDistribution') }}</span>
            <span class="panel-sub">Composition</span>
          </div>
          <div class="panel-body chart-body">
            <v-chart :option="pieOption" class="chart-320" autoresize />
          </div>
        </div>
      </el-col>
    </el-row>

    <!-- Row 3: 最近执行 + 安全告警 -->
    <el-row :gutter="20" class="mb-24">
      <el-col :span="14">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-icon"><el-icon><Document /></el-icon></span>
            <span class="panel-title">{{ $t('dashboard.panel.recentExec') }}</span>
            <span class="panel-sub">Recent commands</span>
            <div class="panel-actions">
              <el-button text size="small" @click="$router.push('/log-exec')">
                {{ $t('dashboard.viewAll') }}<el-icon class="ml-2"><ArrowRight /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="panel-body compact">
            <el-table :data="recentExec" size="default" class="w-full">
              <el-table-column prop="log_name" :label="$t('dashboard.col.user')" width="100">
                <template #default="{ row }">
                  <span class="mono-text">{{ row.log_name }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="log_type" :label="$t('dashboard.col.type')" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="execTypeTag(row.log_type)" effect="plain">
                    {{ execTypeLabel(row.log_type) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="log_info" :label="$t('dashboard.col.detail')" show-overflow-tooltip>
                <template #default="{ row }">
                  <span class="mono-text text-sm">{{ row.log_info }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="log_host" :label="$t('dashboard.col.asset')" width="120" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ execTargetLabel(row) }}
                </template>
              </el-table-column>
              <el-table-column prop="log_status" :label="$t('dashboard.col.status')" width="80" align="center">
                <template #default="{ row }">
                  <span :class="['status-pill', execStatusClass(row.log_status)]">
                    <span class="dot"></span>{{ statusLabel(row.log_status) }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column prop="log_time" :label="$t('dashboard.col.time')" width="140">
                <template #default="{ row }">
                  <span class="text-sm">{{ row.log_time }}</span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </el-col>
      <el-col :span="10">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-icon panel-icon-danger">
              <el-icon><Warning /></el-icon>
            </span>
            <span class="panel-title">{{ $t('dashboard.panel.securityAlerts') }}</span>
            <el-tag v-if="failCount > 0" size="small" type="danger" effect="dark" round>{{ failCount }}</el-tag>
            <div class="panel-actions">
              <el-button text size="small" @click="$router.push('/log-login')">
                {{ $t('dashboard.viewAll') }}<el-icon class="ml-2"><ArrowRight /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="panel-body" style="padding:0">
            <div v-if="!securityAlerts.length" class="alert-empty">
              <div class="alert-empty-icon">
                <el-icon :size="28" color="#10B981"><CircleCheck /></el-icon>
              </div>
              <span>{{ $t('dashboard.noAbnormalLogin') }}</span>
              <span class="text-xs" style="margin-top:2px">{{ $t('dashboard.securityGood') }}</span>
            </div>
            <div v-else class="alert-list">
              <div v-for="(item, i) in securityAlerts" :key="i" class="alert-item">
                <span class="status-dot offline mr-12"></span>
                <div class="alert-info">
                  <div class="alert-top">
                    <span class="alert-user">{{ item.log_name }}</span>
                    <span class="alert-detail mono-text">{{ item.log_gw_ip || item.log_nw_ip || '-' }}</span>
                  </div>
                  <span class="alert-reason">{{ item.log_reason || $t('dashboard.loginFailed') }}</span>
                </div>
                <span class="alert-time">{{ item.log_time }}</span>
              </div>
            </div>
          </div>
        </div>
      </el-col>
    </el-row>

    <!-- Row 4: AI 运维执行 + 定时任务 + 资产组分布 + 登录Top -->
    <el-row :gutter="20">
      <el-col :span="6">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-icon"><el-icon><Cpu /></el-icon></span>
            <span class="panel-title">{{ $t('dashboard.panel.aiOpsExec') }}</span>
            <span class="panel-sub">Last 7 days</span>
            <div class="panel-actions">
              <el-button text size="small" @click="$router.push('/ai-ops')">
                <el-icon><ArrowRight /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="panel-body chart-body">
            <div v-if="aiExecTotal === 0" class="alert-empty" style="min-height:240px">
              <span>{{ $t('dashboard.aiExecEmpty') }}</span>
              <span class="text-xs" style="margin-top:2px">{{ $t('dashboard.aiExecEmptyHint') }}</span>
            </div>
            <v-chart v-else :option="aiExecOption" class="chart-240" autoresize />
          </div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-icon"><el-icon><Timer /></el-icon></span>
            <span class="panel-title">{{ $t('menu.cron') }}</span>
            <div class="panel-actions">
              <el-button text size="small" @click="$router.push('/cron')">
                <el-icon><ArrowRight /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="panel-body cron-flex">
            <v-chart :option="cronOption" class="chart-180" autoresize />
            <div class="cron-summary">
              <div class="cron-stat">
                <span class="cron-dot success"></span>
                <span class="cron-label">{{ $t('dashboard.cron.running') }}</span>
                <span class="cron-value num">{{ cronRunning }}</span>
              </div>
              <div class="cron-stat">
                <span class="cron-dot warning"></span>
                <span class="cron-label">{{ $t('dashboard.cron.paused') }}</span>
                <span class="cron-value num">{{ cronPaused }}</span>
              </div>
              <div class="cron-stat">
                <span class="cron-dot info"></span>
                <span class="cron-label">{{ $t('dashboard.cron.total') }}</span>
                <span class="cron-value num">{{ cronRunning + cronPaused }}</span>
              </div>
            </div>
          </div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-icon"><el-icon><DataLine /></el-icon></span>
            <span class="panel-title">{{ $t('dashboard.panel.groupDistribution') }}</span>
          </div>
          <div class="panel-body chart-body">
            <v-chart :option="groupBarOption" class="chart-240" autoresize />
          </div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="panel">
          <div class="panel-head">
            <span class="panel-icon"><el-icon><Position /></el-icon></span>
            <span class="panel-title">{{ $t('dashboard.panel.loginTop') }}</span>
          </div>
          <div class="panel-body chart-body">
            <v-chart :option="loginTopOption" class="chart-240" autoresize />
          </div>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, PieChart as PieChartType, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import type { EChartsOption } from 'echarts'
import VChart from 'vue-echarts'
import { ArrowRight, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { getAiProviders, getAiStats, getCountList, getChartUpdate, getChartCount, getLogs, getCronList, getHostList, getLoginIpTop, isAuthDead } from '@/api'
import { store } from '@/store'
import { t } from '@/i18n'
import { statusLabel } from '@/utils/logStatus'

use([CanvasRenderer, LineChart, PieChartType, BarChart, GridComponent, TooltipComponent, LegendComponent])

// ===== 类型 =====
type ThemeKey = 'blue' | 'orange' | 'black'

/** 统计卡 label 的 i18n key（模板 $t(item.labelKey) 即时响应语言切换） */
type StatLabelKey =
  | 'dashboard.stat.assets'
  | 'dashboard.stat.models'
  | 'dashboard.stat.users'
  | 'dashboard.stat.groups'

/** tooltip 存 key + 参数，模板渲染时求值，保证语言切换即时生效 */
interface StatTooltip {
  key:
    | 'dashboard.aiNoPermission'
    | 'dashboard.aiCurrentDefault'
    | 'dashboard.aiConfigureHint'
    | 'dashboard.aiStatusLoadFail'
  params?: Record<string, string>
}

interface StatCard {
  labelKey: StatLabelKey
  value: string | number
  sub: string
  icon: string
  color: string
  route: string
  tooltip?: StatTooltip
}

interface ExecLogRow {
  log_name: string
  log_type: string
  log_info?: string
  log_host?: string
  log_status: string
  log_time: string
  log_gw_ip?: string
  log_nw_ip?: string
  log_reason?: string
  [k: string]: unknown
}

interface CronListItem {
  job_status: string
  [k: string]: unknown
}

interface HostListItem {
  group?: string
  [k: string]: unknown
}

interface CountListResp {
  code: number
  host_len?: number
  user_len?: number
  group_len?: number
  [k: string]: unknown
}

interface AiProviderItem {
  provider_code: string
  display_name?: string
  model?: string
  is_default?: boolean
  available?: boolean
}

interface AiProvidersResp {
  code: number
  providers?: AiProviderItem[]
  default_provider?: string | null
}

interface AiStatsResp {
  code: number
  days?: string[]
  success?: number[]
  failed?: number[]
  total?: number
  [k: string]: unknown
}

interface ChartCountResp {
  code: number
  date_msg?: string[]
  login_msg?: number[]
  user_msg?: number[]
  logerr_msg?: number[]
  [k: string]: unknown
}

interface ExecLogListResp {
  log_list_msg?: ExecLogRow[]
  [k: string]: unknown
}

interface CronListResp {
  cron_list_msg?: CronListItem[]
  [k: string]: unknown
}

interface HostListResp {
  host_list_msg?: HostListItem[]
  [k: string]: unknown
}

interface LoginIpTopResp {
  code: number
  ip_msg?: string[]
  cnt_msg?: number[]
  [k: string]: unknown
}

interface Palette {
  stats: string[]
  line: string[]
  lineArea: string[][]
  category: string[]
  splitLine: string
}

const trendRange = ref<string>('30d')

// ====== 统计卡配色（随主题调整，保证与主题调性一致） ======
const stats = ref<StatCard[]>([
  { labelKey: 'dashboard.stat.assets', value: '-', sub: 'Servers',  icon: 'Monitor', color: '#0EA5E9', route: '/host-list' },
  {
    labelKey: 'dashboard.stat.models',
    value: '-',
    sub: 'AI Providers',
    // 与侧栏「AI 运维」保持同一图标，建立视觉关联
    icon: 'Cpu',
    color: '#06B6D4',
    route: '/ai-ops',
  },
  { labelKey: 'dashboard.stat.users',  value: '-', sub: 'Users',    icon: 'User',   color: '#8B5CF6', route: '/user-list' },
  { labelKey: 'dashboard.stat.groups', value: '-', sub: 'Groups',   icon: 'Box',    color: '#EC4899', route: '/group-list' },
])

/** tooltip 渲染（t 读取全局 locale ref，模板内调用可随语言即时更新） */
function statTooltip(item: StatCard): string {
  return item.tooltip ? t(item.tooltip.key, item.tooltip.params || {}) : ''
}

/** 最近执行状态 → 徽章 class（比较的是后端协议值，非展示文案） */
function execStatusClass(status: string): 'success' | 'warning' | 'danger' {
  if (status === '成功') return 'success' // i18n-ignore
  if (['部分失败', '部分成功'].includes(status)) return 'warning' // i18n-ignore
  return 'danger'
}

// ====== 主题调色板（REV34-M11：所有图表同主题同步响应） ======
//   blue   — 冷调费低素色（走 #3B82F6 / #06B6D4 / #6366F1 / #A78BFA）
//   orange — 原鲜明（走 #3B82F6 / #10B981 / #F97316 / #8B5CF6）
//   black  — 黑主题亮色（走 #38BDF8 / #34D399 / #FB923C / #A78BFA）
// 所有 chart option 中的 series 颜色 / areaStyle 透明梯度 / splitLine 调色 都走这一份调色板
const _CHART_PALETTES: Record<ThemeKey, Palette> = {
  blue: {
    stats:    ['#3B82F6', '#0EA5E9', '#6366F1', '#A78BFA'],
    // 趋势线 3 条 (登录/活跃/失败) + 透明渐变填充
    line:     ['#3B82F6', '#0EA5E9', '#F87171'],
    lineArea: [
      ['rgba(59,130,246,0.18)', 'rgba(59,130,246,0)'],
      ['rgba(14,165,233,0.15)', 'rgba(14,165,233,0)'],
      ['rgba(248,113,113,0.12)', 'rgba(248,113,113,0)'],
    ],
    // 饼图 / 柱图 category 色
    category: ['#3B82F6', '#0EA5E9', '#10B981', '#F97316', '#8B5CF6', '#EC4899', '#06B6D4', '#84CC16'],
    // 折线 splitLine 轴线
    splitLine: '#E0E7FF',
  },
  orange: {
    stats:    ['#3B82F6', '#10B981', '#F97316', '#8B5CF6'],
    line:     ['#F97316', '#10B981', '#EF4444'],
    lineArea: [
      ['rgba(249,115,22,0.18)', 'rgba(249,115,22,0)'],
      ['rgba(16,185,129,0.15)', 'rgba(16,185,129,0)'],
      ['rgba(239,68,68,0.12)', 'rgba(239,68,68,0)'],
    ],
    category: ['#3B82F6', '#10B981', '#F97316', '#8B5CF6', '#EC4899', '#06B6D4', '#F59E0B', '#EF4444'],
    splitLine: '#F0EFEE',
  },
  black: {
    stats:    ['#38BDF8', '#34D399', '#FB923C', '#A78BFA'],
    line:     ['#38BDF8', '#34D399', '#F87171'],
    lineArea: [
      ['rgba(56,189,248,0.20)', 'rgba(56,189,248,0)'],
      ['rgba(52,211,153,0.18)', 'rgba(52,211,153,0)'],
      ['rgba(248,113,113,0.15)', 'rgba(248,113,113,0)'],
    ],
    category: ['#38BDF8', '#34D399', '#FB923C', '#A78BFA', '#EC4899', '#06B6D4', '#F59E0B', '#F87171'],
    splitLine: 'rgba(255,255,255,0.08)',
  },
}

function _areaStyle(colorStops: string[]): { color: { type: string; x: number; y: number; x2: number; y2: number; colorStops: { offset: number; color: string }[] } } {
  return { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: colorStops[0] }, { offset: 1, color: colorStops[1] }] } }
}

function _applyChartTheme(theme: string | undefined): void {
  const palette: Palette = _CHART_PALETTES[(theme as ThemeKey)] || _CHART_PALETTES.orange
  // 1. 统计卡颜色
  stats.value.forEach((s, i) => { s.color = palette.stats[i] })
  // 2. 折线图 3 条 line + 透明填充
  if (lineOption.value.series && Array.isArray(lineOption.value.series) && lineOption.value.series.length === 3) {
    for (let i = 0; i < 3; i++) {
      const s = lineOption.value.series[i] as { itemStyle?: { color: string }; areaStyle?: unknown }
      s.itemStyle = { color: palette.line[i] }
      s.areaStyle = _areaStyle(palette.lineArea[i])
    }
    const yAxis = lineOption.value.yAxis as { splitLine?: { lineStyle: { color: string; type: string } } }
    yAxis.splitLine = { lineStyle: { color: palette.splitLine, type: 'dashed' } }
  }
  // 3. 饼图 / 柱图 调色
  const updateSplit = (opt: { yAxis?: unknown; xAxis?: unknown }): void => {
    if (opt.yAxis) {
      const y = opt.yAxis as { splitLine?: { lineStyle: { color: string; type: string } } }
      y.splitLine = { lineStyle: { color: palette.splitLine, type: 'dashed' } }
    }
    if (opt.xAxis) {
      const x = opt.xAxis as { splitLine?: { lineStyle: { color: string; type: string } } }
      x.splitLine = { lineStyle: { color: palette.splitLine, type: 'dashed' } }
    }
  }
  updateSplit(groupBarOption.value as unknown as { yAxis?: unknown; xAxis?: unknown })
  updateSplit(loginTopOption.value as unknown as { yAxis?: unknown; xAxis?: unknown })
  // 4. AI 执行堆叠柱：成功随主题绿色系、失败随主题红色系
  updateSplit(aiExecOption.value as unknown as { yAxis?: unknown; xAxis?: unknown })
  const aiSeries = aiExecOption.value.series
  if (Array.isArray(aiSeries) && aiSeries.length === 2) {
    const [ok, bad] = aiSeries as Array<{ itemStyle?: Record<string, unknown> }>
    ok.itemStyle = { ...ok.itemStyle, color: palette.line[1] }
    bad.itemStyle = { ...bad.itemStyle, color: palette.line[2] }
  }
}

// ====== 趋势折线 ======
const lineOption = ref<EChartsOption>({
  tooltip: {
    trigger: 'axis',
    backgroundColor: 'rgba(24, 24, 27, 0.95)',
    borderWidth: 0,
    textStyle: { color: '#FAFAF9', fontSize: 12 },
    padding: [10, 14],
  },
  grid: { left: '3%', right: '4%', top: '12%', bottom: '15%', containLabel: true },
  legend: {
    // 图表文案在 option 构建时求值：语言切换后刷新页面（重新 setup）生效
    data: [t('dashboard.chart.loginCount'), t('dashboard.chart.activeUsers'), t('dashboard.chart.loginFail')],
    bottom: 0,
    itemGap: 24,
    icon: 'roundRect',
    itemWidth: 10,
    itemHeight: 10,
    textStyle: { fontSize: 12, color: '#71717A' },
  },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    data: [],
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#A8A29E', fontSize: 11 },
  },
  yAxis: {
    type: 'value',
    splitLine: { lineStyle: { color: '#F0EFEE', type: 'dashed' } },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#A8A29E', fontSize: 11 },
  },
  series: [
    { name: t('dashboard.chart.loginCount'), type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
      data: [], itemStyle: { color: '#F97316' }, lineStyle: { width: 2 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [{ offset: 0, color: 'rgba(249,115,22,0.18)' }, { offset: 1, color: 'rgba(249,115,22,0)' }] } } },
    { name: t('dashboard.chart.activeUsers'), type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
      data: [], itemStyle: { color: '#10B981' }, lineStyle: { width: 2 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [{ offset: 0, color: 'rgba(16,185,129,0.15)' }, { offset: 1, color: 'rgba(16,185,129,0)' }] } } },
    { name: t('dashboard.chart.loginFail'), type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
      data: [], itemStyle: { color: '#EF4444' }, lineStyle: { width: 2 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [{ offset: 0, color: 'rgba(239,68,68,0.12)' }, { offset: 1, color: 'rgba(239,68,68,0)' }] } } },
  ],
})

// ====== 分布饼图 ======
const pieOption = ref<EChartsOption>({
  tooltip: { trigger: 'item', backgroundColor: 'rgba(24, 24, 27, 0.95)', borderWidth: 0, textStyle: { color: '#FAFAF9' } },
  legend: { bottom: '5%', left: 'center', icon: 'circle', itemWidth: 8, textStyle: { color: '#71717A', fontSize: 12 } },
  series: [{
    type: 'pie',
    radius: ['52%', '76%'],
    avoidLabelOverlap: false,
    itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
    // UI修复：0 值扇区不渲染百分比标签（如容器数量为 0 时不再显示浮动 "0%"）
    label: { show: true, formatter: (params: { value?: unknown; percent?: number }): string => (params.value ? `${params.percent}%` : ''), fontSize: 12, color: '#57534E', fontWeight: 600 },
    labelLine: { length: 8, length2: 8 },
    emphasis: { label: { fontSize: 14, fontWeight: 'bold' }, scale: true, scaleSize: 6 },
    data: [],
  }],
})

const recentExec = ref<ExecLogRow[]>([])
const securityAlerts = ref<ExecLogRow[]>([])
const failCount = computed<number>(() => securityAlerts.value.length)
const cronRunning = ref<number>(0)
const cronPaused = ref<number>(0)

const cronOption = ref<EChartsOption>({
  tooltip: { trigger: 'item' },
  series: [{
    type: 'pie',
    radius: ['62%', '82%'],
    label: { show: false },
    emphasis: { label: { show: true, fontSize: 13, fontWeight: 'bold' } },
    itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 3 },
    data: [],
  }],
})

const groupBarOption = ref<EChartsOption>({
  tooltip: { trigger: 'axis', backgroundColor: 'rgba(24, 24, 27, 0.95)', borderWidth: 0, textStyle: { color: '#FAFAF9' } },
  grid: { left: '3%', right: '8%', top: '8%', bottom: '3%', containLabel: true },
  xAxis: {
    type: 'value',
    splitLine: { lineStyle: { color: '#F0EFEE', type: 'dashed' } },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#A8A29E', fontSize: 11 },
  },
  yAxis: {
    type: 'category',
    data: [],
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#57534E', fontSize: 12 },
  },
  series: [{
    type: 'bar',
    data: [],
    barWidth: 14,
    itemStyle: { borderRadius: [0, 4, 4, 0], color: '#3B82F6' },
  }],
})

// ====== AI 运维执行（近 7 天成功/失败堆叠柱） ======
const aiExecTotal = ref<number>(0)
const aiExecOption = ref<EChartsOption>({
  tooltip: { trigger: 'axis', backgroundColor: 'rgba(24, 24, 27, 0.95)', borderWidth: 0, textStyle: { color: '#FAFAF9', fontSize: 12 } },
  grid: { left: '3%', right: '4%', top: '10%', bottom: '18%', containLabel: true },
  legend: { data: [t('common.status.success'), t('common.status.fail')], bottom: 0, itemGap: 24, icon: 'roundRect', itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 12, color: '#71717A' } },
  xAxis: {
    type: 'category',
    data: [],
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#A8A29E', fontSize: 11 },
  },
  yAxis: {
    type: 'value',
    minInterval: 1,
    splitLine: { lineStyle: { color: '#F0EFEE', type: 'dashed' } },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#A8A29E', fontSize: 11 },
  },
  series: [
    { name: t('common.status.success'), type: 'bar', stack: 'ai', data: [], barWidth: 14, itemStyle: { color: '#10B981' } },
    { name: t('common.status.fail'), type: 'bar', stack: 'ai', data: [], barWidth: 14, itemStyle: { borderRadius: [4, 4, 0, 0], color: '#EF4444' } },
  ],
})

const loginTopOption = ref<EChartsOption>({
  tooltip: { trigger: 'axis', backgroundColor: 'rgba(24, 24, 27, 0.95)', borderWidth: 0, textStyle: { color: '#FAFAF9' } },
  grid: { left: '3%', right: '8%', top: '8%', bottom: '3%', containLabel: true },
  xAxis: {
    type: 'value',
    splitLine: { lineStyle: { color: '#F0EFEE', type: 'dashed' } },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#A8A29E', fontSize: 11 },
  },
  yAxis: {
    type: 'category',
    data: [],
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#57534E', fontSize: 12 },
  },
  series: [{
    type: 'bar',
    data: [],
    barWidth: 14,
    itemStyle: { borderRadius: [0, 4, 4, 0], color: '#F97316' },
  }],
})

// 所有 option ref 初始化后再立即应用主题，避免 setup 阶段触发 TDZ。
watch(() => store.theme.current, _applyChartTheme, { immediate: true })

async function refresh(): Promise<void> {
  await loadAll()
}

async function loadAll(): Promise<void> {
  await Promise.all([
    loadStats(),
    loadAiStatus(),
    loadAiExecStats(),
    loadTrend(),
    loadRecentExec(),
    loadSecurityAlerts(),
    loadCronSummary(),
    loadGroupDistribution(),
    loadLoginTop(),
  ])
}

async function loadAiExecStats(): Promise<void> {
  try {
    const res = (await getAiStats()) as unknown as AiStatsResp
    if (res.code !== 0) return
    aiExecTotal.value = Number(res.total || 0)
    const oldSeries = aiExecOption.value.series as Array<{ data: number[] }>
    aiExecOption.value = {
      ...aiExecOption.value,
      xAxis: { ...(aiExecOption.value.xAxis as object), data: res.days || [] },
      series: [
        { ...oldSeries[0], data: res.success || [] },
        { ...oldSeries[1], data: res.failed || [] },
      ],
    }
  } catch {
    // 后端旧版本无 /ai/stats 或无权限时静默：面板保持空态文案
  }
}

// 资源分布饼图数据由两个异步接口共同填充（平台计数 + AI 模型数），谁后到谁重建
const pieCounts = ref<{ host: number; user: number; group: number; model: number }>({
  host: 0, user: 0, group: 0, model: 0,
})

function updateResourcePie(): void {
  const counts = pieCounts.value
  const data = [
    { value: counts.host, name: t('dashboard.chart.assets'), itemStyle: { color: '#3B82F6' } },
    { value: counts.user, name: t('dashboard.chart.users'), itemStyle: { color: '#F97316' } },
    { value: counts.group, name: t('dashboard.chart.groups'), itemStyle: { color: '#8B5CF6' } },
  ]
  // 与「可用模型」统计卡同色；audit 角色或未配置模型时不出现该扇区
  if (counts.model > 0) {
    data.push({ value: counts.model, name: t('dashboard.chart.models'), itemStyle: { color: '#06B6D4' } })
  }
  pieOption.value = {
    ...pieOption.value,
    series: [{ ...(pieOption.value.series as object[])[0], data }],
  }
}

async function loadStats(): Promise<void> {
  try {
    const countRes = (await getCountList()) as unknown as CountListResp
    if (countRes.code === 0) {
      stats.value[0].value = countRes.host_len ?? '-'
      stats.value[2].value = countRes.user_len ?? '-'
      stats.value[3].value = countRes.group_len ?? '-'
      pieCounts.value.host = Number(countRes.host_len || 0)
      pieCounts.value.user = Number(countRes.user_len || 0)
      pieCounts.value.group = Number(countRes.group_len || 0)
      updateResourcePie()
    }
  } catch { if (!isAuthDead()) ElMessage.error(t('dashboard.loadFail.stats')) }
}

async function loadAiStatus(): Promise<void> {
  if (store.user.role === 'audit') {
    stats.value[1].value = '-'
    stats.value[1].tooltip = { key: 'dashboard.aiNoPermission' }
    stats.value[1].route = ''
    return
  }
  try {
    const res = (await getAiProviders()) as unknown as AiProvidersResp
    const providers = res.code === 0 && Array.isArray(res.providers) ? res.providers : []
    const available = providers.filter(item => item.available)
    const current = available.find(item => item.provider_code === res.default_provider)
      || available.find(item => item.is_default)
      || available[0]
    stats.value[1].value = available.length
    stats.value[1].tooltip = current
      ? {
          key: 'dashboard.aiCurrentDefault',
          params: { name: `${current.display_name || current.provider_code}${current.model ? ` · ${current.model}` : ''}` },
        }
      : { key: 'dashboard.aiConfigureHint' }
    pieCounts.value.model = available.length
    updateResourcePie()
  } catch {
    stats.value[1].value = 0
    stats.value[1].tooltip = { key: 'dashboard.aiStatusLoadFail' }
  }
}

async function loadTrend(): Promise<void> {
  try {
    await getChartUpdate()
    const chartRes = (await getChartCount()) as unknown as ChartCountResp
    if (chartRes.code === 0) {
      const oldSeries = lineOption.value.series as Array<{ data: number[] }>
      lineOption.value = {
        ...lineOption.value,
        xAxis: { ...(lineOption.value.xAxis as object), data: chartRes.date_msg || [] },
        series: [
          { ...oldSeries[0], data: chartRes.login_msg || [] },
          { ...oldSeries[1], data: chartRes.user_msg || [] },
          { ...oldSeries[2], data: chartRes.logerr_msg || [] },
        ],
      }
    }
  } catch { if (!isAuthDead()) ElMessage.error(t('dashboard.loadFail.trend')) }
}

async function loadRecentExec(): Promise<void> {
  try {
    // REV35-L16: 服务端分页，取首页 limit 条即可 (后端 log_list_msg 已是分页后结果)
    const res = (await getLogs({ log_type: 'command', page: 1, limit: 8 } as unknown as Record<string, unknown>)) as unknown as ExecLogListResp
    if (res.log_list_msg) recentExec.value = res.log_list_msg
  } catch { if (!isAuthDead()) ElMessage.error(t('dashboard.loadFail.recentExec')) }
}

async function loadSecurityAlerts(): Promise<void> {
  try {
    // REV35-L16: 服务端分页 + 客户端过滤失败状态，取首页 limit 条
    const res = (await getLogs({ log_type: 'login', page: 1, limit: 10 } as unknown as Record<string, unknown>)) as unknown as ExecLogListResp
    if (res.log_list_msg) {
      securityAlerts.value = res.log_list_msg.filter(l => l.log_status !== '成功').slice(0, 6) // i18n-ignore
    }
  } catch { if (!isAuthDead()) ElMessage.error(t('dashboard.loadFail.alerts')) }
}

async function loadCronSummary(): Promise<void> {
  try {
    const res = (await getCronList()) as unknown as CronListResp
    if (res.cron_list_msg) {
      cronRunning.value = res.cron_list_msg.filter(c => c.job_status === '启动').length // i18n-ignore
      cronPaused.value = res.cron_list_msg.filter(c => c.job_status === '暂停').length // i18n-ignore
      cronOption.value = {
        ...cronOption.value,
        series: [{
          ...(cronOption.value.series as object[])[0],
          data: [
            { value: cronRunning.value, name: t('dashboard.cron.running'), itemStyle: { color: '#10B981' } },
            { value: cronPaused.value, name: t('dashboard.cron.paused'), itemStyle: { color: '#F59E0B' } },
          ],
        }],
      }
    }
  } catch { if (!isAuthDead()) ElMessage.error(t('dashboard.loadFail.cron')) }
}

async function loadGroupDistribution(): Promise<void> {
  try {
    const res = (await getHostList()) as unknown as HostListResp
    if (res.host_list_msg) {
      const groupMap: Record<string, number> = {}
      res.host_list_msg.forEach(h => {
        const g = h.group || t('dashboard.ungrouped')
        groupMap[g] = (groupMap[g] || 0) + 1
      })
      const sorted = Object.entries(groupMap).sort((a, b) => b[1] - a[1]).slice(0, 8)
      const oldYAxis = groupBarOption.value.yAxis as { data?: string[] }
      const oldSeries = groupBarOption.value.series as Array<{ data: number[] }>
      groupBarOption.value = {
        ...groupBarOption.value,
        yAxis: { ...oldYAxis, data: sorted.map(s => s[0]) },
        series: [{ ...oldSeries[0], data: sorted.map(s => Number(s[1])) }],
      }
    }
  } catch { if (!isAuthDead()) ElMessage.error(t('dashboard.loadFail.group')) }
}

async function loadLoginTop(): Promise<void> {
  try {
    // REV34-M12: 后端聚合 IP Top N 接口（取代客户端拉 50 条 + JS 聚合样本估算）
    const res = (await getLoginIpTop({ limit: 5 } as unknown as Record<string, unknown>)) as unknown as LoginIpTopResp
    if (res.code === 0 && res.ip_msg) {
      const oldYAxis = loginTopOption.value.yAxis as { data?: string[] }
      const oldSeries = loginTopOption.value.series as Array<{ data: number[] }>
      loginTopOption.value = {
        ...loginTopOption.value,
        yAxis: { ...oldYAxis, data: res.ip_msg },
        series: [{ ...oldSeries[0], data: res.cnt_msg || [] }],
      }
    }
  } catch { if (!isAuthDead()) ElMessage.error(t('dashboard.loadFail.loginTop')) }
}

function execTypeLabel(type: string): string {
  if (type === 'AI 批量命令') return t('dashboard.execType.aiBatch') // i18n-ignore
  if (type === '批量命令') return t('dashboard.execType.batch') // i18n-ignore
  if (['command', 'ssh_cmd'].includes(type)) return t('dashboard.execType.command')
  if (['script', 'ssh_script'].includes(type)) return t('dashboard.execType.script')
  return type || t('dashboard.execType.other')
}

function execTypeTag(type: string): 'primary' | 'success' | 'warning' | 'info' {
  if (type === 'AI 批量命令') return 'success' // i18n-ignore
  if (type === '批量命令') return 'primary' // i18n-ignore
  if (['command', 'ssh_cmd'].includes(type)) return 'primary'
  if (['script', 'ssh_script'].includes(type)) return 'warning'
  return 'info'
}

function execTargetLabel(row: ExecLogRow): string {
  if (row.log_host) return row.log_host
  if (!['AI 批量命令', '批量命令'].includes(row.log_type)) return '—' // i18n-ignore
  const matched = String(row.log_reason || '').match(/(?:^|;\s*)targets=(\d{1,3})(?:;|$)/)
  return matched ? t('dashboard.targetHosts', { n: matched[1] }) : t('dashboard.execType.batch')
}

onMounted(loadAll)
</script>

<style scoped>
/* =========================================
 *  统计卡片（核心签名元素）
 * ========================================= */
.stat-card {
  position: relative;
  background: var(--ogs-surface);
  border: 1px solid var(--ogs-border-subtle);
  border-radius: var(--ogs-radius-md);
  padding: 22px 22px 20px;
  cursor: pointer;
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
  overflow: hidden;
  height: 100%;
}
.stat-card:hover {
  transform: translateY(-2px);
  border-color: var(--ogs-border);
  box-shadow: var(--ogs-shadow);
}
.stat-card .stat-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 18px;
}

.stat-card .stat-meta {
  display: flex;
  align-items: center;
  gap: 9px;
}
.stat-card .stat-icon {
  width: 38px; height: 38px;
  border-radius: var(--ogs-radius);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.stat-card .stat-arrow {
  opacity: 0;
  transform: translateX(-4px);
  transition: all 0.2s;
}
.stat-card:hover .stat-arrow {
  opacity: 1;
  transform: translateX(0);
}
.stat-card .stat-value {
  font-size: 36px;
  font-weight: 700;
  color: var(--ogs-text);
  line-height: 1;
  letter-spacing: -0.02em;
  margin-bottom: 6px;
}
.stat-card .stat-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--ogs-text-secondary);
  letter-spacing: 0.01em;
}
.stat-card .stat-sub {
  font-size: 11px;
  color: var(--ogs-text-muted);
  margin-top: 4px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-weight: 500;
}
/* 顶部光带（签名元素） */
.stat-card .stat-accent {
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  background: linear-gradient(90deg, var(--accent), color-mix(in srgb, var(--accent) 30%, transparent) 60%, transparent 100%);
  opacity: 0.85;
  transition: opacity 0.2s, height 0.2s;
}
.stat-card:hover .stat-accent {
  opacity: 1;
  height: 4px;
}

/* =========================================
 *  安全告警
 * ========================================= */
.alert-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 48px 0;
  color: var(--ogs-text-secondary);
  font-size: 13px;
}
.alert-empty-icon {
  width: 56px; height: 56px;
  border-radius: 50%;
  background: var(--ogs-success-soft);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 8px;
}
.alert-list { max-height: 340px; overflow-y: auto; }
.alert-item {
  display: flex;
  align-items: center;
  padding: 14px 20px;
  border-bottom: 1px solid var(--ogs-border-subtle);
  transition: background 0.15s;
}
.alert-item:last-child { border-bottom: none; }
.alert-item:hover { background: var(--ogs-bg-sunken); }
.alert-info { flex: 1; min-width: 0; }
.alert-top {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 4px;
}
.alert-user { font-size: 13px; font-weight: 600; color: var(--ogs-text); }
.alert-detail { font-size: 12px; color: var(--ogs-text-secondary); }
.alert-reason {
  font-size: 12px;
  color: var(--ogs-text-muted);
}
.alert-time {
  font-size: 11px;
  color: var(--ogs-text-muted);
  flex-shrink: 0;
  margin-left: 12px;
  font-family: var(--ogs-mono);
  font-variant-numeric: tabular-nums;
}

/* =========================================
 *  状态徽章（用于表格内）
 * ========================================= */
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.4;
}
.status-pill .dot {
  width: 5px; height: 5px;
  border-radius: 50%;
}
.status-pill.success {
  background: var(--ogs-success-soft);
  color: var(--ogs-success);
}
.status-pill.success .dot { background: var(--ogs-success); }
.status-pill.danger {
  background: var(--ogs-danger-soft);
  color: var(--ogs-danger);
}
.status-pill.danger .dot { background: var(--ogs-danger); }
.status-pill.warning {
  background: var(--ogs-warning-soft);
  color: var(--ogs-warning);
}
.status-pill.warning .dot { background: var(--ogs-warning); }

/* =========================================
 *  定时任务概览
 * ========================================= */
.cron-summary {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  width: 100%;
}
.cron-stat {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 12px 6px;
  border-radius: var(--ogs-radius-sm);
  background: var(--ogs-bg-sunken);
}
.cron-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  margin-bottom: 2px;
}
.cron-dot.success { background: var(--ogs-success); }
.cron-dot.warning { background: var(--ogs-warning); }
.cron-dot.info    { background: var(--ogs-info); }
.cron-label {
  font-size: 11px;
  color: var(--ogs-text-muted);
  letter-spacing: 0.04em;
}
.cron-value {
  font-size: 18px;
  font-weight: 700;
  color: var(--ogs-text);
}

/* =========================================
 *  黑主题适配
 * ========================================= */
[data-theme="black"] .stat-card { background: var(--ogs-bg-elevated); }
[data-theme="black"] .stat-card .stat-value { color: var(--ogs-text); }
[data-theme="black"] .cron-stat { background: rgba(255,255,255,0.03); }
[data-theme="black"] .alert-item { border-bottom-color: var(--ogs-border); }
[data-theme="black"] .alert-item:hover { background: rgba(255,255,255,0.03); }
</style>
