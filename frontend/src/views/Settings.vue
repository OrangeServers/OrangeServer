<template>
  <div>
    <div class="page-header">
      <div>
        <span class="page-eyebrow">{{ $t('settings.eyebrow') }}</span>
        <h2>{{ $t('settings.title') }}</h2>
        <p class="page-subtitle">{{ $t('settings.subtitle') }}</p>
      </div>
      <div class="page-actions">
        <el-button @click="reloadActiveTab" :loading="loading || providerLoading" plain>
          <el-icon><Refresh /></el-icon><span>{{ $t('settings.reload') }}</span>
        </el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="settings-tabs">
      <el-tab-pane :label="$t('settings.tabs.security')" name="security">
    <!-- 安全设置 -->
    <div class="section-card">
      <div class="section-card-head">
        <span class="section-card-icon"><el-icon><Lock /></el-icon></span>
        <div class="section-card-titles">
          <span class="section-card-title">{{ $t('settings.security.cardTitle') }}</span>
          <span class="section-card-sub">{{ $t('settings.security.cardSub') }}</span>
        </div>
      </div>
      <div class="section-card-body">
        <el-form label-width="0" label-position="top">
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.security.loginTimeout') }}</label>
            <div class="form-row-control">
              <el-input-number v-model="form.login_time" :min="1" :max="1440" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.security.loginTimeoutHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.security.loginFailLimit') }}</label>
            <div class="form-row-control">
              <el-input-number v-model="form.login_fail_limit" :min="1" :max="20" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.security.loginFailLimitHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.security.lockDuration') }}</label>
            <div class="form-row-control">
              <el-input-number v-model="form.lock_duration" :min="1" :max="1440" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.security.lockDurationHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.security.passwordExpire') }}</label>
            <div class="form-row-control">
              <el-input-number v-model="form.password_expire_days" :min="0" :max="365" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.security.passwordExpireHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.security.passwordComplexity') }}</label>
            <div class="form-row-control">
              <div class="toggle-line">
                <el-switch v-model="passwordComplexityOn" />
                <span class="toggle-state" :class="passwordComplexityOn ? 'is-on' : 'is-off'">
                  {{ passwordComplexityOn ? $t('settings.state.on') : $t('settings.state.off') }}
                </span>
              </div>
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.security.passwordComplexityHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.security.mfa') }}</label>
            <div class="form-row-control">
              <div class="toggle-line">
                <el-switch v-model="mfaOn" />
                <span class="toggle-state" :class="mfaOn ? 'is-on' : 'is-off'">
                  {{ mfaOn ? $t('settings.state.on') : $t('settings.state.off') }}
                </span>
              </div>
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.security.mfaHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.security.register') }}</label>
            <div class="form-row-control">
              <div class="toggle-line">
                <el-switch v-model="registerOn" />
                <span class="toggle-state" :class="registerOn ? 'is-on' : 'is-off'">
                  {{ registerOn ? $t('settings.state.on') : $t('settings.state.off') }}
                </span>
              </div>
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.security.registerHint') }}
              </div>
            </div>
          </div>
        </el-form>
      </div>
    </div>
      </el-tab-pane>

      <el-tab-pane :label="$t('settings.tabs.terminal')" name="terminal">
    <!-- 终端设置 -->
    <div class="section-card">
      <div class="section-card-head">
        <span class="section-card-icon"><el-icon><Monitor /></el-icon></span>
        <div class="section-card-titles">
          <span class="section-card-title">{{ $t('settings.terminal.cardTitle') }}</span>
          <span class="section-card-sub">{{ $t('settings.terminal.cardSub') }}</span>
        </div>
      </div>
      <div class="section-card-body">
        <el-form label-width="0" label-position="top">
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.terminal.sshTimeout') }}</label>
            <div class="form-row-control">
              <el-input-number v-model="form.ssh_timeout" :min="5" :max="300" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.terminal.sshTimeoutHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.terminal.scrollback') }}</label>
            <div class="form-row-control">
              <el-input-number v-model="form.terminal_scrollback" :min="1000" :max="50000" :step="1000" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.terminal.scrollbackHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.terminal.sessionRecord') }}</label>
            <div class="form-row-control">
              <div class="toggle-line">
                <el-switch v-model="sessionRecordOn" />
                <span class="toggle-state" :class="sessionRecordOn ? 'is-on' : 'is-off'">
                  {{ sessionRecordOn ? $t('settings.state.on') : $t('settings.state.off') }}
                </span>
              </div>
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.terminal.sessionRecordHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.terminal.maxSessions') }}</label>
            <div class="form-row-control">
              <el-input-number v-model="form.max_concurrent_sessions" :min="1" :max="20" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.terminal.maxSessionsHint') }}
              </div>
            </div>
          </div>
        </el-form>
      </div>
    </div>
      </el-tab-pane>

      <el-tab-pane :label="$t('settings.tabs.audit')" name="audit">
    <!-- 审计设置 -->
    <div class="section-card">
      <div class="section-card-head">
        <span class="section-card-icon"><el-icon><Document /></el-icon></span>
        <div class="section-card-titles">
          <span class="section-card-title">{{ $t('settings.audit.cardTitle') }}</span>
          <span class="section-card-sub">{{ $t('settings.audit.cardSub') }}</span>
        </div>
      </div>
      <div class="section-card-body">
        <el-form label-width="0" label-position="top">
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.audit.logRetention') }}</label>
            <div class="form-row-control">
              <el-input-number v-model="form.log_retention_days" :min="30" :max="3650" :step="30" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.audit.logRetentionHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.audit.commandAudit') }}</label>
            <div class="form-row-control">
              <div class="toggle-line">
                <el-switch v-model="commandAuditOn" />
                <span class="toggle-state" :class="commandAuditOn ? 'is-on' : 'is-off'">
                  {{ commandAuditOn ? $t('settings.state.on') : $t('settings.state.off') }}
                </span>
              </div>
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.audit.commandAuditHint') }}
              </div>
            </div>
          </div>
        </el-form>
      </div>
    </div>
      </el-tab-pane>

      <el-tab-pane :label="$t('settings.tabs.transfer')" name="transfer">
    <!-- 文件传输设置 -->
    <div class="section-card">
      <div class="section-card-head">
        <span class="section-card-icon"><el-icon><Folder /></el-icon></span>
        <div class="section-card-titles">
          <span class="section-card-title">{{ $t('settings.transfer.cardTitle') }}</span>
          <span class="section-card-sub">{{ $t('settings.transfer.cardSub') }}</span>
        </div>
      </div>
      <div class="section-card-body">
        <el-form label-width="0" label-position="top">
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.transfer.uploadLimit') }}</label>
            <div class="form-row-control">
              <el-input-number v-model="form.upload_size_limit" :min="1" :max="10240" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.transfer.uploadLimitHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.transfer.allowUpload') }}</label>
            <div class="form-row-control">
              <div class="toggle-line">
                <el-switch v-model="allowUploadOn" />
                <span class="toggle-state" :class="allowUploadOn ? 'is-on' : 'is-off'">
                  {{ allowUploadOn ? $t('settings.state.on') : $t('settings.state.off') }}
                </span>
              </div>
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.transfer.allowUploadHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.transfer.allowDownload') }}</label>
            <div class="form-row-control">
              <div class="toggle-line">
                <el-switch v-model="allowDownloadOn" />
                <span class="toggle-state" :class="allowDownloadOn ? 'is-on' : 'is-off'">
                  {{ allowDownloadOn ? $t('settings.state.on') : $t('settings.state.off') }}
                </span>
              </div>
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.transfer.allowDownloadHint') }}
              </div>
            </div>
          </div>
        </el-form>
      </div>
    </div>
      </el-tab-pane>

      <el-tab-pane :label="$t('settings.tabs.notification')" name="notification">
    <!-- 通知设置 -->
    <div class="section-card">
      <div class="section-card-head">
        <span class="section-card-icon"><el-icon><Bell /></el-icon></span>
        <div class="section-card-titles">
          <span class="section-card-title">{{ $t('settings.notification.cardTitle') }}</span>
          <span class="section-card-sub">{{ $t('settings.notification.cardSub') }}</span>
        </div>
      </div>
      <div class="section-card-body">
        <el-form label-width="0" label-position="top">
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.notification.mailNotify') }}</label>
            <div class="form-row-control">
              <div class="toggle-line">
                <el-switch v-model="mailNotifyOn" />
                <span class="toggle-state" :class="mailNotifyOn ? 'is-on' : 'is-off'">
                  {{ mailNotifyOn ? $t('settings.state.on') : $t('settings.state.off') }}
                </span>
              </div>
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.notification.mailNotifyHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.notification.alertEmail') }}</label>
            <div class="form-row-control">
              <el-input v-model="form.alert_email" placeholder="admin@example.com" style="max-width:360px" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.notification.alertEmailHint') }}
              </div>
            </div>
          </div>

          <div class="smtp-settings">
            <div class="smtp-settings-head">
              <div>
                <strong>{{ $t('settings.notification.smtp.title') }}</strong>
                <p>{{ $t('settings.notification.smtp.lead') }}</p>
              </div>
              <el-tag v-if="mailForm.password_configured" type="success" effect="plain">{{ $t('settings.notification.smtp.passwordSaved') }}</el-tag>
            </div>
            <el-form label-position="top" class="smtp-form">
              <el-form-item :label="$t('settings.notification.smtp.preset')">
                <el-radio-group v-model="mailPreset" @change="applyMailPreset">
                  <el-radio-button value="126">126</el-radio-button>
                  <el-radio-button value="163">163</el-radio-button>
                  <el-radio-button value="qq">QQ</el-radio-button>
                  <el-radio-button value="custom">{{ $t('settings.notification.smtp.custom') }}</el-radio-button>
                </el-radio-group>
              </el-form-item>
              <div class="smtp-grid">
                <el-form-item :label="$t('settings.notification.smtp.host')">
                  <el-input v-model="mailForm.smtp_host" :disabled="mailPreset !== 'custom'" placeholder="smtp.example.com" />
                </el-form-item>
                <el-form-item :label="$t('settings.notification.smtp.port')">
                  <el-input-number v-model="mailForm.smtp_port" :min="1" :max="65535" :disabled="mailPreset !== 'custom'" controls-position="right" />
                </el-form-item>
                <el-form-item :label="$t('settings.notification.smtp.security')">
                  <el-select v-model="mailForm.security" :disabled="mailPreset !== 'custom'">
                    <el-option value="ssl" :label="$t('settings.notification.smtp.ssl')" />
                    <el-option value="starttls" :label="$t('settings.notification.smtp.starttls')" />
                    <el-option value="none" :label="$t('settings.notification.smtp.none')" />
                  </el-select>
                </el-form-item>
                <el-form-item :label="$t('settings.notification.smtp.fromEmail')">
                  <el-input v-model="mailForm.from_email" placeholder="name@example.com" />
                </el-form-item>
                <el-form-item :label="$t('settings.notification.smtp.password')">
                  <el-input v-model="mailForm.password" type="password" show-password :placeholder="mailForm.password_configured ? $t('settings.notification.smtp.passwordKeep') : $t('settings.notification.smtp.passwordPlaceholder')" />
                </el-form-item>
                <el-form-item :label="$t('settings.notification.smtp.testRecipient')">
                  <el-input v-model="mailForm.send_to" placeholder="test@example.com" />
                </el-form-item>
              </div>
              <div class="smtp-actions">
                <el-button type="primary" plain :loading="mailSaving" @click="saveMail">{{ $t('settings.notification.smtp.save') }}</el-button>
                <el-button :loading="mailTesting" @click="testMail">{{ $t('settings.notification.smtp.test') }}</el-button>
              </div>
            </el-form>
          </div>
        </el-form>
      </div>
    </div>
      </el-tab-pane>

      <el-tab-pane :label="$t('settings.tabs.appearance')" name="appearance">
    <!-- 外观与语言 -->
    <div class="section-card">
      <div class="section-card-head">
        <span class="section-card-icon"><el-icon><Brush /></el-icon></span>
        <div class="section-card-titles">
          <span class="section-card-title">{{ $t('settings.appearance.cardTitle') }}</span>
          <span class="section-card-sub">{{ $t('settings.appearance.cardSub') }}</span>
        </div>
      </div>
      <div class="section-card-body">
        <div class="theme-swatch-grid">
          <div v-for="tm in themes" :key="tm.value"
               class="theme-swatch"
               :class="{ 'is-active': form.color_matching === tm.value }"
               @click="selectTheme(tm.value)">
            <div class="theme-swatch-preview">
              <div class="theme-swatch-sidebar" :style="{background: tm.sidebar}"></div>
              <div class="theme-swatch-main">
                <div class="theme-swatch-header" :style="{background: tm.header}"></div>
                <div class="theme-swatch-body" :style="{background: tm.body}"></div>
              </div>
            </div>
            <div class="theme-swatch-meta">
              <el-icon :size="14" :color="tm.color"><component :is="tm.icon" /></el-icon>
              <span>{{ $t(tm.labelKey) }}</span>
              <span class="theme-swatch-sub">{{ tm.value.toUpperCase() }}</span>
            </div>
            <div class="theme-swatch-check"><el-icon><Check /></el-icon></div>
          </div>
        </div>
        <div class="form-row-hint" style="margin-top:16px">
          <el-icon><InfoFilled /></el-icon>
          {{ $t('settings.appearance.themeHint') }}
        </div>

        <!-- 界面语言：选择即 setLocale 即时预览，落库仍走统一 save() -->
        <div class="form-row" style="margin-top:22px">
          <label class="form-row-label">{{ $t('settings.appearance.languageLabel') }}</label>
          <div class="form-row-control">
            <el-radio-group :model-value="form.language" @update:model-value="selectLanguage(String($event))">
              <el-radio-button v-for="lang in languageOptions" :key="lang.value" :value="lang.value">
                {{ lang.label }}
              </el-radio-button>
            </el-radio-group>
            <div class="form-row-hint">
              <el-icon><InfoFilled /></el-icon>
              {{ $t('settings.appearance.languageHint') }}
            </div>
          </div>
        </div>
      </div>
    </div>
      </el-tab-pane>

      <el-tab-pane :label="$t('settings.tabs.ai')" name="ai">
    <!-- AI 模型服务 -->
    <div class="section-card">
      <div class="section-card-head">
        <span class="section-card-icon"><el-icon><Cpu /></el-icon></span>
        <div class="section-card-titles">
          <span class="section-card-title">{{ $t('settings.ai.cardTitle') }}</span>
          <span class="section-card-sub">{{ $t('settings.ai.cardSub') }}</span>
        </div>
      </div>
      <div class="section-card-body">
        <div class="provider-intro">
          <el-icon><InfoFilled /></el-icon>
          {{ $t('settings.ai.intro') }}
        </div>
        <div class="provider-workbench" v-loading="providerLoading">
          <nav class="provider-template-list" :aria-label="$t('settings.ai.templateAria')">
            <div class="provider-template-head">
              <span>{{ $t('settings.ai.templateHead') }}</span>
              <small>{{ aiProviders.length }} PROVIDERS</small>
            </div>
            <button
              v-for="provider in aiProviders"
              :key="provider.provider_code"
              type="button"
              class="provider-template"
              :class="{ active: provider.provider_code === activeProviderCode }"
              @click="activeProviderCode = provider.provider_code"
            >
              <span class="provider-template-mark">
                <svg viewBox="0 0 24 24" width="18" height="18" :fill="providerBrandColor(provider.provider_code)" v-html="providerIcon(provider.provider_code)" />
              </span>
              <span class="provider-template-copy">
                <span class="provider-template-title">
                  <strong>{{ provider.name }}</strong>
                  <el-tag size="small" :type="providerStatus(provider).type">
                    {{ providerStatus(provider).label }}
                  </el-tag>
                </span>
                <small>{{ provider.base_url || $t('settings.ai.noBaseUrl') }}</small>
                <small v-if="provider.note" class="provider-note">{{ provider.note }}</small>
              </span>
            </button>
          </nav>

          <div v-if="activeProvider" class="provider-editor">
            <div class="provider-editor-head">
              <div>
                <span class="provider-editor-eyebrow">{{ activeProvider.provider_code }}</span>
                <h3>{{ activeProvider.name }}</h3>
                <p>{{ $t('settings.ai.editorDesc') }}</p>
              </div>
              <el-tag :type="providerStatus(activeProvider).type">
                {{ providerStatus(activeProvider).label }}
              </el-tag>
            </div>

            <el-form label-position="top" class="provider-form">
              <el-form-item label="Base URL">
                <el-input v-model="activeProvider.base_url" placeholder="https://api.example.com/v1" />
              </el-form-item>
              <div class="provider-field-heading">
                <span>{{ $t('settings.ai.modelName') }}</span>
                <el-button
                  class="model-discovery-action"
                  :loading="activeProvider.models_loading"
                  @click="fetchProviderModels(activeProvider)"
                >
                  <el-icon><Refresh /></el-icon>
                  {{ activeProvider.models_loading ? $t('settings.ai.fetchingModels') : $t('settings.ai.fetchModels') }}
                </el-button>
              </div>
              <el-form-item class="provider-model-field">
                <el-select
                  v-model="activeProvider.model"
                  :aria-label="$t('settings.ai.modelName')"
                  filterable
                  allow-create
                  default-first-option
                  class="provider-model-select"
                  :placeholder="$t('settings.ai.modelPlaceholder')"
                  :loading="activeProvider.models_loading"
                  :no-data-text="$t('settings.ai.modelNoData')"
                >
                  <el-option
                    v-for="model in providerModels[activeProvider.provider_code] || []"
                    :key="model"
                    :label="model"
                    :value="model"
                  />
                </el-select>
                <div class="provider-field-hint">{{ $t('settings.ai.modelHint') }}</div>
              </el-form-item>
              <el-form-item :label="$t('settings.ai.contextLabel')">
                <div
                  class="provider-context-options"
                  role="radiogroup"
                  :aria-label="$t('settings.ai.contextLabel')"
                >
                  <button
                    type="button"
                    class="provider-context-option"
                    :class="{ active: activeProvider.context_window_tokens === AI_CONTEXT_TOKENS_STANDARD }"
                    role="radio"
                    :aria-checked="activeProvider.context_window_tokens === AI_CONTEXT_TOKENS_STANDARD"
                    @click="activeProvider.context_window_tokens = AI_CONTEXT_TOKENS_STANDARD"
                  >
                    <span class="provider-context-option-head">
                      <strong>{{ $t('settings.ai.contextStandard') }}</strong>
                      <code>256K</code>
                    </span>
                    <small>{{ $t('settings.ai.contextStandardDesc') }}</small>
                  </button>
                  <button
                    type="button"
                    class="provider-context-option"
                    :class="{ active: activeProvider.context_window_tokens === AI_CONTEXT_TOKENS_DEEP }"
                    role="radio"
                    :aria-checked="activeProvider.context_window_tokens === AI_CONTEXT_TOKENS_DEEP"
                    @click="activeProvider.context_window_tokens = AI_CONTEXT_TOKENS_DEEP"
                  >
                    <span class="provider-context-option-head">
                      <strong>{{ $t('settings.ai.contextDeep') }}</strong>
                      <code>1M</code>
                    </span>
                    <small>{{ $t('settings.ai.contextDeepDesc') }}</small>
                  </button>
                </div>
                <div class="provider-field-hint">
                  {{ $t('settings.ai.contextHint') }}
                </div>
              </el-form-item>
              <el-form-item label="API Key" class="provider-secret-field">
                <el-input
                  :model-value="providerKeyDisplay(activeProvider)"
                  type="password"
                  :show-password="!activeProvider.api_key_configured || activeProvider.api_key_editing"
                  :placeholder="activeProvider.api_key_configured ? $t('settings.ai.keyPlaceholderKeep') : $t('settings.ai.keyPlaceholderNew')"
                  autocomplete="new-password"
                  @focus="beginProviderKeyEdit(activeProvider)"
                  @blur="finishProviderKeyEdit(activeProvider)"
                  @update:model-value="updateProviderKey(activeProvider, String($event))"
                />
                <div
                  class="provider-secret-hint"
                  :class="{
                    configured: activeProvider.api_key_configured && !activeProvider.api_key_editing,
                    pending: Boolean(activeProvider.api_key),
                  }"
                  aria-live="polite"
                >
                  <el-icon><Lock /></el-icon>
                  <span v-if="activeProvider.api_key">{{ $t('settings.ai.keyPending') }}</span>
                  <span v-else-if="activeProvider.api_key_configured && !activeProvider.api_key_editing">
                    {{ $t('settings.ai.keySaved') }}
                  </span>
                  <span v-else-if="activeProvider.api_key_configured">
                    {{ $t('settings.ai.keyReplacing') }}
                  </span>
                  <span v-else>{{ $t('settings.ai.keyServerOnly') }}</span>
                </div>
              </el-form-item>
              <el-collapse>
                <el-collapse-item :title="$t('settings.ai.advanced')">
                  <el-input
                    v-model="activeProvider.extra_body_text"
                    type="textarea"
                    :rows="5"
                    placeholder="{}"
                    class="provider-json"
                  />
                </el-collapse-item>
              </el-collapse>
              <div class="provider-switches">
                <label>
                  <el-switch
                    v-model="activeProvider.enabled"
                    :loading="activeProvider.saving"
                    @change="setProviderEnabled(activeProvider)"
                  />
                  {{ $t('common.action.enable') }}
                </label>
                <label>
                  <el-switch
                    v-model="activeProvider.is_default"
                    @change="setDefaultProvider(activeProvider)"
                  />
                  {{ $t('settings.ai.defaultSwitch') }}
                </label>
              </div>
              <div class="provider-actions">
                <el-button
                  type="primary"
                  :loading="activeProvider.saving"
                  @click="saveAndEnableProvider(activeProvider)"
                >{{ $t('settings.ai.saveEnable') }}</el-button>
                <el-button
                  plain
                  :loading="activeProvider.saving"
                  @click="saveProvider(activeProvider)"
                >{{ $t('settings.ai.saveOnly') }}</el-button>
                <el-button
                  :loading="activeProvider.testing"
                  @click="testProvider(activeProvider)"
                ><el-icon><Connection /></el-icon>{{ $t('settings.ai.testTool') }}</el-button>
                <el-button
                  v-if="activeProvider.api_key_configured"
                  type="danger"
                  text
                  @click="clearProviderKey(activeProvider)"
                ><el-icon><Delete /></el-icon>{{ $t('settings.ai.clearKey') }}</el-button>
              </div>
            </el-form>
          </div>
          <div v-else class="provider-editor-empty">
            <el-icon><Cpu /></el-icon>
            <span>{{ $t('settings.ai.emptySelect') }}</span>
          </div>
        </div>
      </div>
    </div>
      </el-tab-pane>

      <el-tab-pane :label="$t('settings.tabs.system')" name="system">
    <!-- 系统信息 -->
    <div class="section-card">
      <div class="section-card-head">
        <span class="section-card-icon"><el-icon><Setting /></el-icon></span>
        <div class="section-card-titles">
          <span class="section-card-title">{{ $t('settings.system.cardTitle') }}</span>
          <span class="section-card-sub">{{ $t('settings.system.cardSub') }}</span>
        </div>
      </div>
      <div class="section-card-body">
        <el-form label-width="0" label-position="top">
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.system.systemName') }}</label>
            <div class="form-row-control">
              <el-input v-model="form.system_name" placeholder="OrangeServer" style="max-width:360px" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.system.systemNameHint') }}
              </div>
            </div>
          </div>
          <div class="form-row">
            <label class="form-row-label">{{ $t('settings.system.loginNotice') }}</label>
            <div class="form-row-control">
              <el-input v-model="form.login_notice" type="textarea" :rows="3" :placeholder="$t('settings.system.loginNoticePlaceholder')" style="max-width:480px" />
              <div class="form-row-hint">
                <el-icon><InfoFilled /></el-icon>
                {{ $t('settings.system.loginNoticeHint') }}
              </div>
            </div>
          </div>
        </el-form>
      </div>
    </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 保存 -->
    <div v-if="activeTab !== 'ai'" class="setting-footer">
      <el-button type="primary" size="large" @click="save" :loading="saving">
        <el-icon><Check /></el-icon><span>{{ $t('settings.saveAll') }}</span>
      </el-button>
      <span class="footer-hint">{{ $t('settings.saveAllHint') }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock, Monitor, Document, Folder, Bell, Brush, Setting, Check, Refresh, InfoFilled, Sunny, Cloudy, Moon, Cpu, Connection, Delete } from '@element-plus/icons-vue'
import type { Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getMailSettings, getSettings, testMailSettings, updateMailSettings, updateSettings } from '@/api'
import { applyTheme } from '@/store'
import { t, setLocale, currentLocale } from '@/i18n'
import { aiJsonRequest } from '@/utils/aiStream'
import {
  AI_CONTEXT_TOKENS_DEEP,
  AI_CONTEXT_TOKENS_STANDARD,
} from '@/types/ai'

/** 设置表单 (后端 settings 返回结构) */
interface SettingsForm {
  color_matching: string
  login_time: number
  register_status: string
  login_fail_limit: number
  lock_duration: number
  password_expire_days: number
  mfa_enabled: string
  password_complexity: string
  ssh_timeout: number
  terminal_scrollback: number
  session_record: string
  max_concurrent_sessions: number
  log_retention_days: number
  command_audit: string
  upload_size_limit: number
  allow_upload: string
  allow_download: string
  mail_notify: string
  alert_email: string
  system_name: string
  login_notice: string
  language: string
}

/** 主题选项 */
interface ThemeOption {
  value: string
  labelKey: string
  sidebar: string
  header: string
  body: string
  icon: Component
  color: string
}

interface AiProviderConfig {
  provider_code: string
  name: string
  base_url: string
  model: string
  note?: string
  context_window_tokens: number
  enabled: boolean
  is_default: boolean
  api_key_configured: boolean
  extra_body: Record<string, unknown>
  api_key: string
  extra_body_text: string
  persisted_enabled: boolean
  persisted_is_default: boolean
  saving?: boolean
  testing?: boolean
  models_loading?: boolean
  api_key_editing?: boolean
  last_test?: 'success' | 'failed'
}

interface MailSettingsForm {
  smtp_host: string
  smtp_port: number
  security: 'ssl' | 'starttls' | 'none'
  from_email: string
  password: string
  password_configured: boolean
  send_to: string
}

const MAIL_PRESETS = {
  '126': { smtp_host: 'smtp.126.com', smtp_port: 465, security: 'ssl' },
  '163': { smtp_host: 'smtp.163.com', smtp_port: 465, security: 'ssl' },
  qq: { smtp_host: 'smtp.qq.com', smtp_port: 465, security: 'ssl' },
} as const

const loading = ref<boolean>(false)
const saving = ref<boolean>(false)
const mailLoading = ref<boolean>(false)
const mailSaving = ref<boolean>(false)
const mailTesting = ref<boolean>(false)
const providerLoading = ref<boolean>(false)
const aiProviders = ref<AiProviderConfig[]>([])
const activeProviderCode = ref<string>('')
const providerModels = ref<Record<string, string[]>>({})
const activeProvider = computed<AiProviderConfig | undefined>(() =>
  aiProviders.value.find(provider => provider.provider_code === activeProviderCode.value),
)
import { providerBrandColor, providerIcon } from '@/assets/provider-logos'
const route = useRoute()
const router = useRouter()
const settingTabs = ['security', 'terminal', 'audit', 'transfer', 'notification', 'appearance', 'ai', 'system'] as const
type SettingTab = typeof settingTabs[number]
const requestedTab = typeof route.query.tab === 'string' ? route.query.tab : ''
const activeTab = ref<SettingTab>(settingTabs.includes(requestedTab as SettingTab) ? requestedTab as SettingTab : 'security')
const mailPreset = ref<keyof typeof MAIL_PRESETS | 'custom'>('126')
const mailForm = ref<MailSettingsForm>({
  smtp_host: 'smtp.126.com', smtp_port: 465, security: 'ssl', from_email: '', password: '', password_configured: false, send_to: '',
})

// 开关型字段的布尔代理
const registerOn = ref<boolean>(true)
const mfaOn = ref<boolean>(false)
const passwordComplexityOn = ref<boolean>(false)
const sessionRecordOn = ref<boolean>(true)
const commandAuditOn = ref<boolean>(true)
const allowUploadOn = ref<boolean>(true)
const allowDownloadOn = ref<boolean>(true)
const mailNotifyOn = ref<boolean>(false)

const form = ref<SettingsForm>({
  color_matching: 'orange',
  login_time: 30,
  register_status: 'on',
  login_fail_limit: 5,
  lock_duration: 30,
  password_expire_days: 90,
  mfa_enabled: 'off',
  password_complexity: 'off',
  ssh_timeout: 30,
  terminal_scrollback: 10000,
  session_record: 'on',
  max_concurrent_sessions: 3,
  log_retention_days: 180,
  command_audit: 'on',
  upload_size_limit: 500,
  allow_upload: 'on',
  allow_download: 'on',
  mail_notify: 'off',
  alert_email: '',
  system_name: 'OrangeServer',
  login_notice: '',
  language: currentLocale(),
})

// 主题色块（与 store/applyTheme 的主题 key 一一对应）
const themes: ThemeOption[] = [
  { value: 'orange', labelKey: 'settings.appearance.themes.orange', sidebar: '#1E2A3A', header: '#FFFFFF', body: '#F0F2F5', icon: Sunny,  color: '#FF7A45' },
  { value: 'blue',   labelKey: 'settings.appearance.themes.blue',   sidebar: '#0E3A5C', header: '#2980B9', body: '#EEF6FB', icon: Cloudy, color: '#2980B9' },
  { value: 'black',  labelKey: 'settings.appearance.themes.black',  sidebar: '#1A1A1A', header: '#2D2D2D', body: '#1A1A1A', icon: Moon,   color: '#9CA3AF' },
]

// 语言选项：显示名按各自语言写死，不随界面语言翻译（locale picker 惯例）
const languageOptions = [
  { value: 'zh-CN', label: '简体中文' }, // i18n-ignore
  { value: 'en-US', label: 'English' },
]

// on/off 字段 ↔ 布尔值的映射
function toBool(val: string | undefined): boolean { return val === 'on' }
function toOnOff(val: boolean): string { return val ? 'on' : 'off' }

// 同步布尔开关到 form
function syncSwitchesToForm(): void {
  form.value.register_status = toOnOff(registerOn.value)
  form.value.mfa_enabled = toOnOff(mfaOn.value)
  form.value.password_complexity = toOnOff(passwordComplexityOn.value)
  form.value.session_record = toOnOff(sessionRecordOn.value)
  form.value.command_audit = toOnOff(commandAuditOn.value)
  form.value.allow_upload = toOnOff(allowUploadOn.value)
  form.value.allow_download = toOnOff(allowDownloadOn.value)
  form.value.mail_notify = toOnOff(mailNotifyOn.value)
}

// 从 form 同步到布尔开关
function syncFormToSwitches(): void {
  registerOn.value = toBool(form.value.register_status)
  mfaOn.value = toBool(form.value.mfa_enabled)
  passwordComplexityOn.value = toBool(form.value.password_complexity)
  sessionRecordOn.value = toBool(form.value.session_record)
  commandAuditOn.value = toBool(form.value.command_audit)
  allowUploadOn.value = toBool(form.value.allow_upload)
  allowDownloadOn.value = toBool(form.value.allow_download)
  mailNotifyOn.value = toBool(form.value.mail_notify)
}

async function loadSettings(): Promise<void> {
  loading.value = true
  try {
    const res = (await getSettings()) as unknown as Partial<SettingsForm>
    form.value = {
      color_matching: res.color_matching || 'orange',
      login_time: res.login_time || 30,
      register_status: res.register_status || 'on',
      login_fail_limit: res.login_fail_limit ?? 5,
      lock_duration: res.lock_duration ?? 30,
      password_expire_days: res.password_expire_days ?? 90,
      mfa_enabled: res.mfa_enabled || 'off',
      password_complexity: res.password_complexity || 'off',
      ssh_timeout: res.ssh_timeout ?? 30,
      terminal_scrollback: res.terminal_scrollback ?? 10000,
      session_record: res.session_record || 'on',
      max_concurrent_sessions: res.max_concurrent_sessions ?? 3,
      log_retention_days: res.log_retention_days ?? 180,
      command_audit: res.command_audit || 'on',
      upload_size_limit: res.upload_size_limit ?? 500,
      allow_upload: res.allow_upload || 'on',
      allow_download: res.allow_download || 'on',
      mail_notify: res.mail_notify || 'off',
      alert_email: res.alert_email || '',
      system_name: res.system_name || 'OrangeServer',
      login_notice: res.login_notice || '',
      language: res.language || currentLocale(),
    }
    syncFormToSwitches()
  } finally { loading.value = false }
}

async function reloadActiveTab(): Promise<void> {
  if (activeTab.value === 'ai') {
    await Promise.all([loadSettings(), loadAiProviders()])
    return
  }
  if (activeTab.value === 'notification') {
    await Promise.all([loadSettings(), loadMailSettings()])
    return
  }
  await loadSettings()
}

function mailPresetFor(form: Pick<MailSettingsForm, 'smtp_host' | 'smtp_port' | 'security'>): keyof typeof MAIL_PRESETS | 'custom' {
  return (Object.entries(MAIL_PRESETS) as Array<[keyof typeof MAIL_PRESETS, typeof MAIL_PRESETS[keyof typeof MAIL_PRESETS]]>)
    .find(([, preset]) => preset.smtp_host === form.smtp_host && preset.smtp_port === form.smtp_port && preset.security === form.security)?.[0] || 'custom'
}

function applyMailPreset(): void {
  if (mailPreset.value === 'custom') return
  Object.assign(mailForm.value, MAIL_PRESETS[mailPreset.value])
}

function mailPayload(includeRecipient = false): Record<string, unknown> {
  const { smtp_host, smtp_port, security, from_email, password, send_to } = mailForm.value
  return {
    smtp_host: smtp_host.trim(), smtp_port, security, from_email: from_email.trim(), password,
    ...(includeRecipient && send_to.trim() ? { send_to: send_to.trim() } : {}),
  }
}

async function loadMailSettings(): Promise<void> {
  mailLoading.value = true
  try {
    const res = await getMailSettings() as unknown as Partial<MailSettingsForm> & { code?: number }
    if (res.code !== 0) return
    mailForm.value = {
      smtp_host: res.smtp_host || '', smtp_port: res.smtp_port || 587, security: res.security || 'starttls',
      from_email: res.from_email || '', password: '', password_configured: Boolean(res.password_configured), send_to: '',
    }
    mailPreset.value = mailPresetFor(mailForm.value)
  } catch { ElMessage.error(t('settings.notification.smtp.loadFail')) }
  finally { mailLoading.value = false }
}

async function saveMail(): Promise<void> {
  mailSaving.value = true
  try {
    const res = await updateMailSettings(mailPayload()) as unknown as { code?: number; msg?: string }
    if (res.code !== 0) { ElMessage.error(res.msg || t('settings.notification.smtp.saveFail')); return }
    mailForm.value.password = ''
    mailForm.value.password_configured = true
    ElMessage.success(t('settings.notification.smtp.saveSuccess'))
  } catch { ElMessage.error(t('settings.notification.smtp.saveFail')) }
  finally { mailSaving.value = false }
}

async function testMail(): Promise<void> {
  mailTesting.value = true
  try {
    const res = await testMailSettings(mailPayload(true)) as unknown as { code?: number; msg?: string }
    if (res.code !== 0) { ElMessage.error(res.msg || t('settings.notification.smtp.testFail')); return }
    ElMessage.success(res.msg || t('settings.notification.smtp.testSuccess'))
  } catch { ElMessage.error(t('settings.notification.smtp.testFail')) }
  finally { mailTesting.value = false }
}

function selectTheme(val: string): void {
  form.value.color_matching = val
  applyTheme(val)
}

// 语言与主题模式交互对齐：选择即 setLocale 即时预览，保存仍走统一 save()
function selectLanguage(val: string): void {
  form.value.language = val
  setLocale(val)
}

async function save(): Promise<void> {
  saving.value = true
  syncSwitchesToForm()
  try {
    // SETTINGS-SAVE-FIX: 后端业务失败返回 200 + code:100，必须检查业务码——
    //   旧实现只捕网络异常，保存被拒也弹“保存成功”（静默失败的根源）
    const res = await updateSettings(form.value as unknown as Record<string, unknown>) as unknown as { code?: number; msg?: string }
    if (res.code !== 0) {
      ElMessage.error(res.msg || t('settings.msg.saveFail'))
      return
    }
    ElMessage.success(t('settings.msg.saveSuccess'))
  } catch { ElMessage.error(t('settings.msg.saveFail')) }
  finally { saving.value = false }
}

async function loadAiProviders(): Promise<void> {
  providerLoading.value = true
  try {
    const response = await aiJsonRequest<{ providers?: AiProviderConfig[]; data?: AiProviderConfig[] }>(
      '/ai/admin/providers',
    )
    const rows = response.providers || response.data || []
    aiProviders.value = rows.map((row) => ({
      ...row,
      context_window_tokens: row.context_window_tokens || AI_CONTEXT_TOKENS_STANDARD,
      api_key: '',
      api_key_editing: false,
      extra_body_text: JSON.stringify(row.extra_body || {}, null, 2),
      persisted_enabled: row.enabled,
      persisted_is_default: row.is_default,
    }))
    if (!aiProviders.value.some(provider => provider.provider_code === activeProviderCode.value)) {
      const preferredProvider =
        aiProviders.value.find(provider => provider.is_default)
        || aiProviders.value.find(provider => provider.enabled && provider.api_key_configured)
        || aiProviders.value.find(provider => provider.enabled)
        || aiProviders.value.find(provider => provider.api_key_configured)
        || aiProviders.value[0]
      activeProviderCode.value = preferredProvider?.provider_code || ''
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('settings.ai.msg.loadFail'))
  } finally {
    providerLoading.value = false
  }
}

function setDefaultProvider(selected: AiProviderConfig): void {
  if (!selected.is_default) return
  selected.enabled = true
  aiProviders.value.forEach((provider) => {
    if (provider.provider_code !== selected.provider_code) provider.is_default = false
  })
}

const SAVED_API_KEY_MASK = 'saved-api-key'

function providerKeyDisplay(provider: AiProviderConfig): string {
  if (provider.api_key_editing || !provider.api_key_configured) return provider.api_key
  return SAVED_API_KEY_MASK
}

function beginProviderKeyEdit(provider: AiProviderConfig): void {
  if (provider.api_key_configured && !provider.api_key) provider.api_key_editing = true
}

function finishProviderKeyEdit(provider: AiProviderConfig): void {
  if (provider.api_key_configured && !provider.api_key) provider.api_key_editing = false
}

function updateProviderKey(provider: AiProviderConfig, value: string): void {
  provider.api_key_editing = true
  provider.api_key = value
}

function providerPayload(provider: AiProviderConfig): Record<string, unknown> {
  let extraBody: Record<string, unknown>
  try {
    const parsed = JSON.parse(provider.extra_body_text || '{}') as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error()
    extraBody = parsed as Record<string, unknown>
  } catch {
    throw new Error(t('settings.ai.msg.extraBodyInvalid', { name: provider.name }))
  }
  return {
    base_url: provider.base_url,
    model: provider.model,
    context_window_tokens: provider.context_window_tokens,
    api_key: provider.api_key,
    enabled: provider.enabled,
    is_default: provider.is_default,
    extra_body: extraBody,
  }
}

function providerDiscoveryPayload(provider: AiProviderConfig): Record<string, unknown> {
  const payload = providerPayload(provider)
  delete payload.model
  delete payload.context_window_tokens
  payload.enabled = provider.persisted_enabled
  payload.is_default = provider.persisted_is_default
  return payload
}

function restorePersistedProviderFlags(): void {
  aiProviders.value.forEach((provider) => {
    provider.enabled = provider.persisted_enabled
    provider.is_default = provider.persisted_is_default
  })
}

function providerStatus(provider: AiProviderConfig): {
  type: 'success' | 'warning' | 'info' | 'danger'
  label: string
} {
  if (provider.last_test === 'failed') return { type: 'danger', label: t('settings.ai.status.testFailed') }
  if (provider.last_test === 'success' && provider.enabled) return { type: 'success', label: t('settings.ai.status.toolOk') }
  if (!provider.api_key_configured && !provider.api_key) return { type: 'warning', label: t('settings.ai.status.noKey') }
  if (!provider.model) return { type: 'warning', label: t('settings.ai.status.noModel') }
  if (!provider.enabled) return { type: 'info', label: t('settings.ai.status.notEnabled') }
  return { type: 'success', label: t('common.status.enabled') }
}

async function saveProvider(provider: AiProviderConfig, silent = false): Promise<boolean> {
  provider.saving = true
  try {
    const response = await aiJsonRequest<{ provider?: AiProviderConfig; data?: AiProviderConfig }>(
      `/ai/admin/providers/${provider.provider_code}`,
      { method: 'PUT', body: providerPayload(provider) },
    )
    const saved = response.provider || response.data
    if (saved) {
      Object.assign(provider, saved, {
        api_key: '',
        api_key_editing: false,
        extra_body_text: JSON.stringify(saved.extra_body || {}, null, 2),
        persisted_enabled: saved.enabled,
        persisted_is_default: saved.is_default,
      })
      if (saved.is_default) {
        aiProviders.value.forEach((candidate) => {
          if (candidate.provider_code === saved.provider_code) return
          candidate.is_default = false
          candidate.persisted_is_default = false
        })
      }
    }
    if (!silent) ElMessage.success(t('settings.ai.msg.providerSaved', { name: provider.name }))
    return true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('settings.msg.saveFail'))
    return false
  } finally {
    provider.saving = false
  }
}

async function saveAndEnableProvider(provider: AiProviderConfig): Promise<void> {
  provider.enabled = true
  if (!await saveProvider(provider)) restorePersistedProviderFlags()
}

async function setProviderEnabled(provider: AiProviderConfig): Promise<void> {
  const persistedEnabled = provider.persisted_enabled
  if (!await saveProvider(provider)) provider.enabled = persistedEnabled
}

async function saveProviderDiscoveryDraft(provider: AiProviderConfig): Promise<boolean> {
  provider.saving = true
  try {
    const response = await aiJsonRequest<{ provider?: AiProviderConfig; data?: AiProviderConfig }>(
      `/ai/admin/providers/${provider.provider_code}`,
      { method: 'PUT', body: providerDiscoveryPayload(provider) },
    )
    const saved = response.provider || response.data
    if (saved) {
      provider.base_url = saved.base_url
      provider.api_key = ''
      provider.api_key_editing = false
      provider.api_key_configured = saved.api_key_configured
      provider.extra_body_text = JSON.stringify(saved.extra_body || {}, null, 2)
      provider.persisted_enabled = saved.enabled
      provider.persisted_is_default = saved.is_default
    }
    return true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('settings.ai.msg.saveDraftFail'))
    return false
  } finally {
    provider.saving = false
  }
}

function unwrapModelList(response: unknown): string[] {
  if (!response || typeof response !== 'object') return []
  const payload = response as Record<string, unknown>
  const direct = payload.models
  if (Array.isArray(direct)) return direct.map(String)
  if (Array.isArray(payload.data)) return payload.data.map(String)
  if (payload.data && typeof payload.data === 'object') {
    const nested = (payload.data as Record<string, unknown>).models
    if (Array.isArray(nested)) return nested.map(String)
  }
  return []
}

async function fetchProviderModels(provider: AiProviderConfig): Promise<void> {
  provider.models_loading = true
  try {
    // 模型枚举使用服务端已保存配置。保存草稿不会自动启用或设为默认。
    if (!await saveProviderDiscoveryDraft(provider)) return
    const response = await aiJsonRequest(
      `/ai/admin/providers/${encodeURIComponent(provider.provider_code)}/models`,
      { method: 'POST' },
    )
    const models = [...new Set(unwrapModelList(response))].sort((a, b) => a.localeCompare(b))
    providerModels.value = { ...providerModels.value, [provider.provider_code]: models }
    if (models.length) {
      ElMessage.success(t('settings.ai.msg.modelsFetched', { n: models.length }))
    } else {
      ElMessage.warning(t('settings.ai.msg.modelsEmpty'))
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('settings.ai.msg.modelsFetchFail'))
  } finally {
    provider.models_loading = false
  }
}

async function testProvider(provider: AiProviderConfig): Promise<void> {
  provider.testing = true
  try {
    if (!await saveProvider(provider)) return
    await aiJsonRequest(`/ai/admin/providers/${provider.provider_code}/test`, {
      method: 'POST',
    })
    provider.last_test = 'success'
    ElMessage.success(t('settings.ai.msg.testPassed', { name: provider.name }))
  } catch (error) {
    provider.last_test = 'failed'
    ElMessage.error(error instanceof Error ? error.message : t('settings.ai.msg.testFail'))
  } finally {
    provider.testing = false
  }
}

async function clearProviderKey(provider: AiProviderConfig): Promise<void> {
  try {
    await ElMessageBox.confirm(
      t('settings.ai.msg.clearConfirm', { name: provider.name }),
      t('settings.ai.msg.clearConfirmTitle'),
      {
        confirmButtonText: t('settings.ai.clearKey'),
        cancelButtonText: t('common.action.cancel'),
        type: 'warning',
      },
    )
  } catch {
    return
  }
  try {
    await aiJsonRequest(`/ai/admin/providers/${provider.provider_code}/clear-key`, {
      method: 'POST',
    })
    provider.api_key_configured = false
    provider.enabled = false
    provider.is_default = false
    provider.persisted_enabled = false
    provider.persisted_is_default = false
    provider.api_key = ''
    provider.api_key_editing = false
    provider.last_test = undefined
    ElMessage.success(t('settings.ai.msg.keyCleared', { name: provider.name }))
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('settings.ai.msg.clearFail'))
  }
}

onMounted(() => {
  void loadSettings()
  void loadMailSettings()
  void loadAiProviders()
})

watch(
  () => route.query.tab,
  (tab) => {
    if (typeof tab === 'string' && settingTabs.includes(tab as SettingTab)) {
      activeTab.value = tab as SettingTab
    }
  },
)

watch(activeTab, (tab) => {
  if (route.query.tab === tab) return
  void router.replace({ query: { ...route.query, tab } })
})
</script>

<style scoped>
/* section-card / form-row / theme-swatch / form-row-hint 全部由全局 styles/index.css 提供 */

.settings-tabs :deep(.el-tabs__header) {
  margin: 0 0 18px;
  padding: 0 14px;
  border: 1px solid var(--ogs-border);
  border-radius: 4px;
  background: var(--ogs-surface);
}

.smtp-settings {
  margin-top: 24px;
  padding-top: 22px;
  border-top: 1px solid var(--ogs-border);
}
.smtp-settings-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.smtp-settings-head strong { color: var(--ogs-text); font-size: 14px; }
.smtp-settings-head p { margin: 5px 0 0; color: var(--ogs-text-muted); font-size: 12px; }
.smtp-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 16px; max-width: 760px; }
.smtp-grid :deep(.el-input-number), .smtp-grid :deep(.el-select) { width: 100%; }
.smtp-actions { display: flex; gap: 10px; }
@media (max-width: 640px) {
  .smtp-settings-head { flex-direction: column; gap: 8px; }
  .smtp-grid { grid-template-columns: 1fr; }
}
.settings-tabs :deep(.el-tabs__nav-wrap::after) {
  height: 1px;
  background: var(--ogs-border-subtle);
}
.settings-tabs :deep(.el-tabs__item) {
  height: 48px;
  color: var(--ogs-text-secondary);
  font-size: 13px;
}
.settings-tabs :deep(.el-tabs__item.is-active) {
  color: var(--ogs-primary);
  font-weight: 600;
}
.settings-tabs :deep(.el-tabs__active-bar) {
  height: 3px;
  border-radius: 3px 3px 0 0;
  background: var(--ogs-primary);
}
.settings-tabs :deep(.el-tab-pane > .section-card) {
  margin-bottom: 0;
}

/* 开关行：el-switch + 状态文字 */
.toggle-line {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding-top: 2px;
}
.toggle-state {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  padding: 2px 9px;
  border-radius: 10px;
  font-family: var(--ogs-mono);
  border: 1px solid transparent;
  transition: all 0.2s;
}
.toggle-state.is-on {
  background: var(--ogs-log-success-soft);
  color: var(--ogs-log-success);
  border-color: rgba(16, 185, 129, 0.22);
}
.toggle-state.is-off {
  background: var(--ogs-bg-sunken);
  color: var(--ogs-text-muted);
  border-color: var(--ogs-border-subtle);
}

/* 保存按钮区 */
.setting-footer {
  margin-top: 28px;
  padding: 24px;
  text-align: center;
  background: var(--ogs-surface);
  border: 1px solid var(--ogs-border);
  border-radius: 4px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.footer-hint {
  font-size: 12px;
  color: var(--ogs-text-muted);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.provider-intro {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 18px;
  padding: 11px 14px;
  border: 1px solid var(--ogs-border-subtle);
  border-radius: var(--ogs-radius-sm);
  background: var(--ogs-bg-sunken);
  color: var(--ogs-text-secondary);
  font-size: 12px;
}
.provider-workbench {
  min-height: 570px;
  display: grid;
  grid-template-columns: 286px minmax(0, 1fr);
  overflow: hidden;
  border: 1px solid var(--ogs-border);
  border-radius: 4px;
  background: var(--ogs-surface);
}
.provider-template-list {
  padding: 14px;
  border-right: 1px solid var(--ogs-border);
  background: var(--ogs-bg-sunken);
}
.provider-template-head {
  padding: 3px 6px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--ogs-text-secondary);
  font-size: 12px;
  font-weight: 600;
}
.provider-template-head small {
  color: var(--ogs-text-muted);
  font: 9px var(--ogs-mono);
  letter-spacing: .06em;
}
.provider-template {
  width: 100%;
  min-width: 0;
  margin-bottom: 7px;
  padding: 11px;
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  color: var(--ogs-text);
  text-align: left;
  border: 1px solid transparent;
  border-radius: var(--ogs-radius-sm);
  background: transparent;
  cursor: pointer;
  transition: border-color .16s, background .16s, box-shadow .16s;
}
.provider-template:hover {
  border-color: var(--ogs-border);
  background: var(--ogs-surface);
}
.provider-template.active {
  border-color: var(--ogs-primary);
  background: var(--ogs-surface);
  box-shadow: 0 5px 16px var(--ogs-primary-soft);
}
.provider-template:focus-visible {
  outline: 3px solid var(--ogs-primary-ring);
  outline-offset: 2px;
}
.provider-template-mark {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: #F0F0F0;
  border: 1px solid rgba(0,0,0,0.06);
  flex-shrink: 0;
}
[data-theme="black"] .provider-template-mark {
  background: #2A2A2A;
  border-color: rgba(255,255,255,0.08);
}
.provider-template-copy { min-width: 0; }
.provider-template-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.provider-template-title strong {
  min-width: 0;
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.provider-template-title :deep(.el-tag) {
  height: 22px;
  flex: 0 0 auto;
  padding-inline: 6px;
  font-size: 10px;
}
.provider-template-copy > small {
  display: block;
  margin-top: 5px;
  overflow: hidden;
  color: var(--ogs-text-muted);
  font: 9px var(--ogs-mono);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.provider-note {
  color: var(--ogs-warning) !important;
  white-space: normal !important;
  line-height: 1.4;
}
.provider-editor {
  min-width: 0;
  padding: 24px clamp(22px, 4vw, 44px) 30px;
}
.provider-editor-head {
  margin-bottom: 24px;
  padding-bottom: 18px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 1px solid var(--ogs-border-subtle);
}
.provider-editor-eyebrow {
  color: var(--ogs-primary);
  font: 700 9px var(--ogs-mono);
  letter-spacing: .1em;
  text-transform: uppercase;
}
.provider-editor-head h3 {
  margin-top: 4px;
  color: var(--ogs-text);
  font-size: 20px;
  letter-spacing: -.015em;
}
.provider-editor-head p {
  margin-top: 6px;
  color: var(--ogs-text-secondary);
  font-size: 11px;
}
.provider-form { max-width: 760px; }
.provider-field-heading {
  width: 100%;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--ogs-text);
  font-size: 13px;
  line-height: 30px;
}
.model-discovery-action {
  min-height: 30px;
  padding: 0 12px;
  color: var(--ogs-text-secondary);
  font-size: 13px;
  font-weight: 600;
  border-color: var(--ogs-border);
  background: var(--ogs-surface);
  box-shadow: none;
}
.model-discovery-action:hover,
.model-discovery-action:focus-visible {
  color: var(--ogs-primary);
  border-color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
}
.provider-model-field {
  margin-bottom: 18px;
}
.provider-model-select { width: 100%; }
.provider-context-options {
  width: 100%;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.provider-context-option {
  min-width: 0;
  padding: 13px 14px;
  color: var(--ogs-text-secondary);
  text-align: left;
  border: 1px solid var(--ogs-border);
  border-radius: var(--ogs-radius-sm);
  background: var(--ogs-surface);
  cursor: pointer;
  transition: border-color .16s, background .16s, box-shadow .16s;
}
.provider-context-option:hover {
  border-color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
}
.provider-context-option.active {
  border-color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
  box-shadow: inset 3px 0 0 var(--ogs-primary);
}
.provider-context-option:focus-visible {
  outline: 3px solid var(--ogs-primary-ring);
  outline-offset: 2px;
}
.provider-context-option-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}
.provider-context-option-head strong {
  color: var(--ogs-text);
  font-size: 13px;
}
.provider-context-option-head code {
  color: var(--ogs-primary);
  font: 700 12px var(--ogs-mono);
}
.provider-context-option small {
  display: block;
  margin-top: 6px;
  color: var(--ogs-text-muted);
  font-size: 10px;
  line-height: 1.5;
}
.provider-field-hint {
  margin-top: 6px;
  color: var(--ogs-text-muted);
  font-size: 10px;
  line-height: 1.5;
}
.provider-secret-hint {
  margin-top: 7px;
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--ogs-text-muted);
  font-size: 11px;
  line-height: 1.45;
}
.provider-secret-hint.configured {
  color: var(--ogs-success);
}
.provider-secret-hint.pending {
  color: var(--ogs-warning);
}
.provider-secret-hint .el-icon {
  flex: 0 0 auto;
  font-size: 12px;
}
.provider-secret-field :deep(.el-input__inner) {
  letter-spacing: .08em;
}
.provider-secret-field :deep(.el-input__inner::placeholder) {
  letter-spacing: normal;
}
.provider-editor-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 9px;
  color: var(--ogs-text-muted);
  font-size: 12px;
}
.provider-editor-empty .el-icon { font-size: 28px; }
.provider-json {
  font-family: var(--ogs-mono);
}
.provider-switches,
.provider-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 14px;
}
.provider-switches label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--ogs-text-secondary);
  font-size: 12px;
}
@media (max-width: 980px) {
  .provider-workbench {
    grid-template-columns: 1fr;
  }
  .provider-template-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 7px;
    border-right: 0;
    border-bottom: 1px solid var(--ogs-border);
  }
  .provider-template-head { grid-column: 1 / -1; }
  .provider-template { margin-bottom: 0; }
}
@media (max-width: 620px) {
  .provider-template-list { grid-template-columns: 1fr; }
  .provider-editor { padding: 20px 15px 24px; }
  .provider-editor-head { flex-direction: column; }
  .provider-field-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
  }
  .model-discovery-action { width: 100%; }
  .provider-context-options { grid-template-columns: 1fr; }
  .provider-actions .el-button { margin-left: 0; }
}
.provider-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}
@media (max-width: 820px) {
  .settings-tabs :deep(.el-tabs__header) {
    padding: 0 8px;
  }
  .settings-tabs :deep(.el-tabs__item) {
    padding: 0 9px;
    font-size: 12px;
  }
}
</style>
