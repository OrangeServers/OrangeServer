// =============================================================================
// zh-CN 语言包（schema 源）：en-US 各命名空间以 satisfies 对齐本文件结构。
// 命名空间按页面/领域拆分为独立模块——并行开发互不冲突。
// 新增命名空间：1) 建 zh-CN/<ns>.ts 与 en-US/<ns>.ts  2) 两侧 index.ts 各挂一行
// =============================================================================
import auth from './auth'
import dashboard from './dashboard'
import assets from './assets'
import users from './users'
import ops from './ops'
import ai from './ai'
import aiRuns from './aiRuns'
import cron from './cron'
import fileTransfer from './fileTransfer'
import ssh from './ssh'
import audit from './audit'
import authority from './authority'
import settings from './settings'
import setup from './setup'
import common from './common'
import layout from './layout'
import menu from './menu'

export default {
  setup,
  auth,
  dashboard,
  assets,
  users,
  ops,
  ai,
  aiRuns,
  cron,
  fileTransfer,
  ssh,
  audit,
  authority,
  settings,
  common,
  layout,
  menu,
}
