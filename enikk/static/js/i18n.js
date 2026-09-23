// i18n module - translations and language utilities

const translations = {
  'zh-CN': {
    config: {
      title: '配置', model: '模型', default_model: '默认模型', fast_model: '轻决策快档模型（可选）', provider: '提供商',
      base_url: 'API 地址', api_key: 'API 密钥', max_tokens: '最大令牌数', context_length: '上下文长度',
      context_length_auto: '自动', context_length_custom: '自定义',
      context_length_builtin_hint: '选择内置 provider 建议选择「自动」',
      testing: '测试中...', test_connection: '测试连接',
      connection_successful: '✓ 连接成功', connection_failed: '✗ 连接失败',
      im_platforms: '即时通讯平台', enabled: '启用', app_id: '应用 ID',
      client_secret: '客户端密钥', workspace: '工作区', screenshot_dir: '截图目录',
      browse_dir: '浏览目录', open_in_explorer: '在文件管理器中打开',
      weights_dir: '权重目录', max_screenshot_dim: '最大截图尺寸',
      max_iterations: '最大迭代次数',
      logging: '日志', log_level: '日志级别', loading: '正在加载配置...',
      cancel: '取消', save: '保存', saving: '保存中...',
      saved_restart: '配置已保存，重启程序后生效',
      failed_load: '加载配置失败', failed_save: '保存配置失败',
      tab_basic: '基本配置', tab_apps: '应用配置', tab_advanced: '高级配置', tab_im: '即时通讯', tab_ioa: 'IOA 服务',
      ioa_tip: '💡 Manufex 桌面自动化依赖的两项服务：远端 OmniParser（界面元素识别）与大模型（决策）。在此填写即可，无需手改配置文件。',
      parser_title: 'OmniParser 视觉解析服务',
      parser_url: '服务地址（PARSER_SERVICE_URL）',
      parser_url_ph: 'http://10.91.66.57:8077',
      parser_token: '访问 Token（X-Auth-Token）',
      parser_token_ph: '共享访问令牌',
      parser_note: '保存后立即生效，优先级高于环境变量。可用 ioa_parser_status 工具验证连通性。',
      model_title: '大模型（决策）',
      model_note: 'Agent 的决策模型走 OpenAI 兼容接口：填写 base_url、api_key、模型名（如 deepseek-v4-flash）。',
      model_goto: '→ 去基本配置填写模型 API',
      basic_tip: '💡 配置 AI 模型的 API 信息。支持 OpenAI 兼容的 API（如 OpenAI、Azure OpenAI、本地模型等）。',
      im_tip: '💡 可选配置。如果你想通过即时通讯平台（如 QQ、钉钉）与 Agent 交互，可以在这里配置对应的平台信息。',
      tab_wecom: '企业微信',
      wecom_tip: '💡 企业微信集成分两部分：群机器人 Webhook（把 Manufex 执行过程实时推送到企微群，只需一个地址，虚拟机无需公网 IP）；自建应用回调（在企微里跟机器人对话来控制虚拟机，双向，但需要公网可达的回调地址）。',
      wecom_webhook_title: '群机器人推送（单向，推荐先配置这个）',
      wecom_webhook_url: 'Webhook 地址',
      wecom_webhook_hint: '企微群 → 群机器人 → 添加机器人 → 复制 Webhook 地址，粘贴到这里即可。',
      wecom_push_all: '推送所有会话进度（不限于企微发起的任务）',
      wecom_notify_tools: '推送工具调用',
      wecom_notify_images: '推送截图',
      wecom_send_test: '发送测试消息',
      wecom_test_ok: '已发送', wecom_test_failed: '发送失败',
      wecom_longconn_title: '长连接双向对话',
      wecom_recommended: '推荐',
      wecom_longconn_enable: '启用长连接双向控制',
      wecom_longconn_hint: '✅ 由本机主动连接企业微信服务器（WebSocket），无需公网 IP、无需端口转发，最适合纯内网虚拟机。企微客户端 → 工作台 → 智能机器人 → 创建机器人 → 手动创建 → API模式创建 → 连接方式选"使用长连接"，即可获得下方的 Bot ID 和 Secret。',
      wecom_refresh_status: '刷新连接状态',
      wecom_status_unknown: '未知',
      wecom_longconn_restart_hint: '保存后需重启 Manufex 生效。连接成功后直接在企微里给机器人发消息即可对话控制。',
      wecom_callback_title: '自建应用回调（双向，需公网可达）',
      wecom_callback_enable: '启用双向对话控制',
      wecom_callback_warning: '⚠️ 需要企业微信服务器能访问到本机的回调地址（公网 IP / 端口转发 / 内网穿透），纯内网虚拟机无法直接使用，需自行做端口转发。上方的长连接模式无此限制，推荐优先使用。',
      wecom_allowed_users: '允许控制的企微 UserId（逗号分隔，留空=不限制）',
      wecom_callback_url_label: '回调地址（填入企微后台"接收消息"配置，将 <你的公网地址> 换成实际可达地址）',
      show: '查看', hide: '隐藏', qq_open_platform: 'QQ开放平台',
      close_behavior: '关闭行为', close_behavior_ask: '每次询问',
      close_behavior_minimize: '最小化到托盘', close_behavior_close: '直接关闭',
      close_behavior_tip: '💡 点击窗口关闭按钮时的行为。选择「最小化到托盘」后，程序会常驻系统托盘，可通过托盘图标重新打开窗口。',
      autostart: '开机自动启动', autostart_enabled: '启用',
      autostart_tip: '💡 开启后 Manufex 将在系统启动时自动运行并最小化到托盘。需要管理员权限。',
      autostart_toggling: '设置中...',
    },
    sidebar: {
      new_chat: '新对话', collapse: '折叠侧边栏', no_conversations: '暂无对话', no_im_sessions: '暂无 IM 会话',
      im_status: '即时通讯连接状态', connected: '已连接', disconnected: '未连接',
      dashboard: '控制台', refresh: '刷新', open_home: '打开 Home 目录', open_logs: '打开日志目录', settings: '设置', exit: '退出', skills: '技能库', cron: '定时任务', cron_sessions: '定时会话', memory: '记忆', knowledge: '知识库',
      tab_chat: '对话', tab_cron: '定时', tab_im: 'IM',
      search_sessions: '搜索对话...',
      language: '语言', rename: '重命名', delete: '删除', rename_failed: '重命名失败',
      confirm_delete: '确定要删除对话「{title}」吗？',
      update_available: '有新版本 v{0}，点击查看',
    },
    skills: {
      search: '搜索 Skills...', browser: 'Skills', empty: '暂无 Skills',
      select_hint: '从左侧选择一个 Skill 查看详情',
    },
    chat: {
      toggle_thinking: '切换思考过程', toggle_tool_calls: '切换工具调用',
      welcome_title: '有什么我可以帮您的？', welcome_subtitle: '开始对话，探索无限可能',
      load_more: '加载更多...', thinking: '思考中...', thought: '思考过程',
      call: '调用', step: '步骤', copy: '复制', message_placeholder: '输入消息...',
      stop: '停止', send: '发送',
      mouse_cursor_tip: '💡 图片中的红色十字表示鼠标当前位置'
    },
    time: { today: '今天', yesterday: '昨天', last_7_days: '最近7天', older: '更早' },
    apps: {
      empty: '还没有配置应用', add: '添加应用', edit: '编辑应用',
      name: '名称', path: '路径', advanced: '高级设置',
      launcher_path: '启动器路径', timeout: '启动超时 (秒)',
      name_required: '请输入应用名称', path_required: '请选择可执行文件',
      save_failed: '保存失败', delete_failed: '删除失败',
      confirm_delete: '确定要删除应用 {name} 吗？',
      description_title: '应用配置说明',
      description_body: '在此注册应用后，AI Agent 可以通过名称快速启动它们。当你告诉 Agent "打开 XXX" 时，它会自动查找已注册的应用并执行完整的启动流程（包括启动器登录、等待加载等）。',
      description_tip: '💡 你也可以直接告诉 Agent 添加应用，它会自动调用 register_app 工具完成注册。',
      advanced_warning: '⚠️ 以下配置项通常无需修改，仅在特殊情况下调整'
    },
    memory: {
      title: '学习配置',
      memory_enabled: '启用自动学习',
      memory_enabled_desc: 'Agent 会在后台自动总结经验',
      nudge_interval: '经验总结间隔',
      nudge_interval_desc: '每 N 次对话后触发经验总结',
      creation_nudge_interval: '技能总结间隔',
      creation_nudge_interval_desc: '每 N 次工具调用后触发技能总结',
      view_title: '记忆', memory_file: 'memory.md', user_file: 'user.md',
      empty: '暂无内容', edit: '编辑', save: '保存', saving: '保存中...', cancel: '取消',
      saved_hint: '已保存，启动新会话后生效',
    },
    knowledge: {
      view_title: '知识库',
      kb_root: '知识目录',
      refresh: '刷新',
      tab_kb: '业务知识', tab_corrections: '错题本', tab_success: '成功路径',
      empty: '暂无内容',
      draft: '待审核',
      review_pass: '审核通过', delete: '删除',
      importing: '导入中...',
      import_done: '导入完成：错题 {0} · 成功路径 {1} · 知识 {2} · 用例 {3}（跳过 {4}）',
      import_failed: '导入失败',
      search_placeholder: '检索知识（如：创建文档 搜索）', search: '检索', searching: '检索中...',
      no_hits: '无命中', hits: '条命中',
      new_correction: '新增错题', title_placeholder: '标题（如：点地址栏后误用 Alt+N 导致输入失败）',
      content_placeholder: '内容：什么场景、错误做法、正确做法…', add: '保存错题', adding: '保存中...',
      added_hint: '已保存到错题本',
      open_file: '点击查看/收起内容',
      select_mode: '多选', select_done: '完成', select_all: '全选/取消',
      selected: '已选', delete_selected: '删除所选', deleting: '删除中...',
    },
    status: {
      icon_finder: 'Icon Finder', ocr: 'OCR', im: 'IM', connected: '已连接', disconnected: '未连接', not_configured: '未配置', cron_disabled: '定时任务已禁用',
      provider: '提供商', model_name: '模型', no_model: '未配置模型', context_length: '上下文长度', context_auto: '自动',
    },
    cron: {
      title: '定时任务',
      new_job: '新建任务',
      prompt: '执行内容',
      schedule: '调度规则',
      schedule_placeholder: '如: every 30m, 0 9 * * *, 2h',
      name: '任务名称',
      deliver: '结果投递',
      deliver_im: 'IM 消息',
      deliver_local: '本地保存',
      repeat: '重复次数',
      repeat_placeholder: '留空=无限',
      max_run_time: '超时(秒)',
      max_run_time_placeholder: '默认600',
      status: '状态',
      scheduled: '已调度',
      paused: '已暂停',
      running: '运行中',
      error: '错误',
      completed: '已完成',
      last_run: '上次运行',
      next_run: '下次运行',
      actions: '操作',
      confirm_delete: '确认删除此定时任务？',
      no_jobs: '暂无定时任务',
      save: '保存',
      cancel: '取消',
      edit: '编辑',
      pause: '暂停',
      resume: '恢复',
      trigger_now: '立即执行',
      delete: '删除',
      history: '执行历史',
      session_count: '{count} 次执行',
      no_sessions: '暂无执行记录',
      view_session: '查看会话',
      view_all_sessions: '查看全部执行记录',
      view_sessions: '查看执行记录',
      filter_active: '正在筛选',
      clear_filter: '清除筛选',
      search_placeholder: '搜索任务名称或 ID...',
    },
    confirm: {
      cancel: '取消', delete: '删除'
    },
    picker: {
      launch: '选择窗口', change: '切换窗口', launching: '正在启动...', tools: '工具',
      success: '✓ 已绑定窗口: {title}', launch_failed: '启动窗口选择器失败',
      unpick_failed: '解绑窗口失败',
    }
  },
  'en': {
    config: {
      title: 'Configuration', model: 'Model', default_model: 'Default Model', fast_model: 'Fast Model (optional)',
      provider: 'Provider', base_url: 'Base URL', api_key: 'API Key', max_tokens: 'Max Tokens', context_length: 'Context Length',
      context_length_auto: 'Auto', context_length_custom: 'Custom',
      context_length_builtin_hint: 'Recommended to use "Auto" for built-in providers',
      testing: 'Testing...', test_connection: 'Test Connection',
      connection_successful: '✓ Connection successful', connection_failed: '✗ Connection failed',
      im_platforms: 'IM Platforms', enabled: 'Enabled', app_id: 'App ID',
      client_secret: 'Client Secret', workspace: 'Workspace', screenshot_dir: 'Screenshot Directory',
      browse_dir: 'Browse directory', open_in_explorer: 'Open in file explorer',
      weights_dir: 'Weights Directory', max_screenshot_dim: 'Max Screenshot Dimension',
      max_iterations: 'Max Iterations',
      logging: 'Logging', log_level: 'Log Level', loading: 'Loading configuration...',
      cancel: 'Cancel', save: 'Save', saving: 'Saving...',
      saved_restart: 'Configuration saved, restart to take effect',
      failed_load: 'Failed to load configuration', failed_save: 'Failed to save configuration',
      tab_basic: 'Basic', tab_apps: 'Apps', tab_advanced: 'Advanced', tab_im: 'IM', tab_ioa: 'IOA Services',
      ioa_tip: '💡 The two services Manufex depends on: remote OmniParser (UI element parsing) and the decision LLM. Fill them in here — no config-file editing needed.',
      parser_title: 'OmniParser Vision Service',
      parser_url: 'Service URL (PARSER_SERVICE_URL)',
      parser_url_ph: 'http://10.91.66.57:8077',
      parser_token: 'Access Token (X-Auth-Token)',
      parser_token_ph: 'shared access token',
      parser_note: 'Takes effect immediately on save; overrides env vars. Use the ioa_parser_status tool to verify connectivity.',
      model_title: 'Decision LLM',
      model_note: 'The agent decision model uses an OpenAI-compatible API: base_url, api_key, model name (e.g. deepseek-v4-flash).',
      model_goto: '→ Configure the model in Basic',
      basic_tip: '💡 Configure your AI model API settings. Supports OpenAI-compatible APIs (OpenAI, Azure OpenAI, local models, etc.).',
      im_tip: '💡 Optional. If you want to interact with the Agent via IM platforms (like QQ, DingTalk), configure them here.',
      tab_wecom: 'WeCom',
      wecom_tip: '💡 WeCom integration has two parts: Group Robot Webhook (push Manufex\'s execution progress to a WeCom group in real time — just one URL, no public IP needed on the VM); Self-built App callback (chat with the bot in WeCom to control the VM, bidirectional, but requires a publicly reachable callback URL).',
      wecom_webhook_title: 'Group Robot Push (one-way, set this up first)',
      wecom_webhook_url: 'Webhook URL',
      wecom_webhook_hint: 'WeCom group → Group Robot → Add Robot → copy the Webhook URL and paste it here.',
      wecom_push_all: 'Push progress for ALL sessions (not just WeCom-originated tasks)',
      wecom_notify_tools: 'Push tool calls',
      wecom_notify_images: 'Push screenshots',
      wecom_send_test: 'Send test message',
      wecom_test_ok: 'Sent', wecom_test_failed: 'Send failed',
      wecom_longconn_title: 'Long-Connection Two-Way Chat',
      wecom_recommended: 'Recommended',
      wecom_longconn_enable: 'Enable long-connection two-way control',
      wecom_longconn_hint: '✅ This machine connects OUT to WeCom\'s server (WebSocket) — no public IP, no port-forward needed. Best for VMs with only a private IP. In the WeCom client: Workspace → Smart Robot → Create Robot → Manual → API mode → connection type "Long connection" to get the Bot ID and Secret below.',
      wecom_refresh_status: 'Refresh status',
      wecom_status_unknown: 'unknown',
      wecom_longconn_restart_hint: 'Restart Manufex after saving. Once connected, just message the bot in WeCom to chat/control.',
      wecom_callback_title: 'Self-built App Callback (two-way, needs public reachability)',
      wecom_callback_enable: 'Enable two-way chat control',
      wecom_callback_warning: '⚠️ Requires WeCom\'s servers to reach this callback URL (public IP / port-forward / reverse tunnel). A VM with only a private IP cannot use this directly. The long-connection mode above has no such requirement — prefer it.',
      wecom_allowed_users: 'Allowed WeCom UserIds (comma-separated, empty = no restriction)',
      wecom_callback_url_label: 'Callback URL (paste into WeCom admin console "Receive Messages", replace <your-public-address> with your real reachable address)',
      show: 'Show', hide: 'Hide', qq_open_platform: 'QQ Open Platform',
      close_behavior: 'Close Behavior', close_behavior_ask: 'Ask every time',
      close_behavior_minimize: 'Minimize to tray', close_behavior_close: 'Close app',
      close_behavior_tip: '💡 What happens when you click the close button. With "Minimize to tray", the app stays running in the system tray and can be reopened from there.',
      autostart: 'Auto-start on Boot', autostart_enabled: 'Enabled',
      autostart_tip: '💡 When enabled, Manufex will start automatically on system boot and minimize to the tray. Requires administrator privileges.',
      autostart_toggling: 'Applying...',
    },
    sidebar: {
      new_chat: 'New Chat', collapse: 'Collapse sidebar', no_conversations: 'No conversations yet', no_im_sessions: 'No IM sessions',
      im_status: 'IM Bridge connection status', connected: 'Connected', disconnected: 'Disconnected',
      dashboard: 'dashboard', refresh: 'Refresh', open_home: 'Open Home directory', open_logs: 'Open Logs directory', settings: 'Settings', exit: 'Exit', skills: 'Skills', cron: 'Cron Jobs', cron_sessions: 'Cron Sessions', memory: 'Memory', knowledge: 'Knowledge',
      tab_chat: 'Chat', tab_cron: 'Cron', tab_im: 'IM',
      search_sessions: 'Search conversations...',
      language: 'Language', rename: 'Rename', delete: 'Delete', rename_failed: 'Rename failed',
      confirm_delete: 'Delete conversation "{title}"?',
      update_available: 'New version v{0} available, click to view',
    },
    skills: {
      search: 'Search skills...', browser: 'Skills', empty: 'No skills found',
      select_hint: 'Select a skill from the sidebar to view details',
    },
    chat: {
      toggle_thinking: 'Toggle thinking', toggle_tool_calls: 'Toggle tool calls',
      welcome_title: 'What can I help you with?', welcome_subtitle: 'Start a conversation and explore infinite possibilities',
      load_more: 'Load more...', thinking: 'Thinking...', thought: 'Thought',
      call: 'call', step: 'Step', copy: 'Copy', message_placeholder: 'Message Manufex',
      stop: 'Stop', send: 'Send',
      mouse_cursor_tip: '💡 The red crosshair in the image indicates the current mouse position'
    },
    time: { today: 'Today', yesterday: 'Yesterday', last_7_days: 'Last 7 days', older: 'Older' },
    apps: {
      empty: 'No apps configured yet', add: 'Add App', edit: 'Edit App',
      name: 'Name', path: 'Path', advanced: 'Advanced Settings',
      launcher_path: 'Launcher Path', timeout: 'Launch Timeout (seconds)',
      name_required: 'Please enter app name', path_required: 'Please select executable file',
      save_failed: 'Save failed', delete_failed: 'Delete failed',
      confirm_delete: 'Are you sure you want to delete app {name}?',
      description_title: 'About App Configuration',
      description_body: 'Register your apps here so the AI Agent can launch them by name. When you tell the Agent to "open XXX", it automatically finds the registered app and executes the full launch flow (including launcher login, loading screens, etc.).',
      description_tip: '💡 You can also ask the Agent to add apps directly — it will call the register_app tool automatically.',
      advanced_warning: '⚠️ These settings typically do not need to be modified. Only adjust if you know what you are doing.'
    },
    memory: {
      title: 'Learning',
      memory_enabled: 'Enable Auto Learning',
      memory_enabled_desc: 'Agent will summarize experiences in the background',
      nudge_interval: 'Experience Summary Interval',
      nudge_interval_desc: 'Trigger experience summary every N conversations',
      creation_nudge_interval: 'Skill Summary Interval',
      creation_nudge_interval_desc: 'Trigger skill summary every N tool calls',
      view_title: 'Memory', memory_file: 'memory.md', user_file: 'user.md',
      empty: 'No content', edit: 'Edit', save: 'Save', saving: 'Saving...', cancel: 'Cancel',
      saved_hint: 'Saved. Changes take effect in a new session.',
    },
    knowledge: {
      view_title: 'Knowledge Base',
      kb_root: 'Root directory',
      refresh: 'Refresh',
      tab_kb: 'Business KB', tab_corrections: 'Corrections', tab_success: 'Success Paths',
      empty: 'No entries yet.',
      draft: 'Draft',
      review_pass: 'Approve', delete: 'Delete',
      importing: 'Importing...',
      import_done: 'Imported: corrections {0} · success paths {1} · KB {2} · cases {3} (skipped {4})',
      import_failed: 'Import failed',
      search_placeholder: 'Search knowledge (e.g. create document)', search: 'Search', searching: 'Searching...',
      no_hits: 'No hits', hits: ' hits',
      new_correction: 'New Correction', title_placeholder: 'Title (what went wrong)',
      content_placeholder: 'Content: scenario, wrong approach, correct approach…', add: 'Save Correction', adding: 'Saving...',
      added_hint: 'Saved to corrections',
      open_file: 'Click to view/collapse content',
      select_mode: 'Select', select_done: 'Done', select_all: 'Select all/none',
      selected: 'selected', delete_selected: 'Delete selected', deleting: 'Deleting...',
    },
    status: {
      icon_finder: 'Icon Finder', ocr: 'OCR', im: 'IM', connected: 'Connected', disconnected: 'Disconnected', not_configured: 'Not configured', cron_disabled: 'Cron disabled',
      provider: 'Provider', model_name: 'Model', no_model: 'No model configured', context_length: 'Context Length', context_auto: 'Auto',
    },
    cron: {
      title: 'Cron Jobs',
      new_job: 'New Job',
      prompt: 'Task',
      schedule: 'Schedule',
      schedule_placeholder: 'e.g. every 30m, 0 9 * * *, 2h',
      name: 'Name',
      deliver: 'Deliver to',
      deliver_im: 'IM Message',
      deliver_local: 'Local Only',
      repeat: 'Repeat',
      repeat_placeholder: 'Empty = forever',
      max_run_time: 'Timeout (sec)',
      max_run_time_placeholder: 'Default 600',
      status: 'Status',
      scheduled: 'Scheduled',
      paused: 'Paused',
      running: 'Running',
      error: 'Error',
      completed: 'Completed',
      last_run: 'Last Run',
      next_run: 'Next Run',
      actions: 'Actions',
      confirm_delete: 'Delete this cron job?',
      no_jobs: 'No cron jobs',
      save: 'Save',
      cancel: 'Cancel',
      edit: 'Edit',
      pause: 'Pause',
      resume: 'Resume',
      trigger_now: 'Trigger now',
      delete: 'Delete',
      history: 'History',
      session_count: '{count} runs',
      no_sessions: 'No execution history',
      view_session: 'View session',
      view_all_sessions: 'View all execution history',
      view_sessions: 'View execution history',
      filter_active: 'Filtering',
      clear_filter: 'Clear filter',
      search_placeholder: 'Search by job name or ID...',
    },
    confirm: {
      cancel: 'Cancel', delete: 'Delete'
    },
    picker: {
      launch: 'Pick Window', change: 'Change Window', launching: 'Launching...', tools: 'Tools',
      success: '✓ Bound to window: {title}', launch_failed: 'Failed to launch window picker',
      unpick_failed: 'Failed to unbind window',
    }
  }
};

function resolveLang(lang) {
  if (translations[lang]) return lang;
  const prefix = lang.split('-')[0];
  for (const key of Object.keys(translations)) {
    if (key.startsWith(prefix)) return key;
  }
  console.warn('[i18n] Unsupported language, falling back to default:', lang);
  return null;
}

let currentLang = 'zh-CN';

// Read language from URL parameter (passed by backend)
const urlLang = new URLSearchParams(window.location.search).get('lang');
if (urlLang) {
  const resolved = resolveLang(urlLang);
  if (resolved) {
    currentLang = resolved;
    console.log('[i18n] Loaded language from URL:', currentLang);
  }
}

function t(key, ...args) {
  const keys = key.split('.');
  let value = translations[currentLang];

  for (const k of keys) {
    if (value && typeof value === 'object') {
      value = value[k];
    } else {
      return key;
    }
  }

  if (value === undefined) return key;

  if (args.length > 0 && typeof value === 'string') {
    return value.replace(/\{(\d+)\}/g, (match, index) => {
      return args[parseInt(index)] !== undefined ? args[parseInt(index)] : match;
    });
  }

  return value;
}

function setLang(lang) {
  const resolved = resolveLang(lang);
  if (resolved) {
    currentLang = resolved;
    window.dispatchEvent(new CustomEvent('language-changed'));
  }
}
