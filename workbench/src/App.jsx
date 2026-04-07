import { useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragOverlay,
} from '@dnd-kit/core';
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  rectSortingStrategy,
  useSortable,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';

function gridClass(count) {
  if (count <= 1) return 'grid-1';
  if (count === 2) return 'grid-2';
  if (count === 3) return 'grid-3';
  if (count === 4) return 'grid-4';
  if (count <= 6) return 'grid-6';
  return 'grid-9';
}

function normalizeMessages(arr = []) {
  return arr
    .map((m) => ({
      role: m.role,
      content: m.content || m.text || '',
      source: m.source || '',
      timestamp: m.timestamp || null
    }))
    .filter((m) => m.content);
}

function formatTime(isoString) {
  if (!isoString) return '';
  try {
    const date = new Date(isoString);
    const now = new Date();
    const isToday = date.toDateString() === now.toDateString();

    const timeStr = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    if (isToday) return timeStr;

    const dateStr = date.toLocaleDateString([], { month: 'short', day: 'numeric' });
    return `${dateStr} ${timeStr}`;
  } catch (e) {
    return '';
  }
}

function makeOptimisticMessage(text) {
  return {
    role: 'user',
    content: text,
    source: 'ui',
    timestamp: new Date().toISOString(),
    optimistic: true,
    optimisticId: `optimistic-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
  };
}


function isCommandInput(text) {
  return /^\/\S/.test((text || '').trim());
}

function reconcileMessages(existing = [], incoming = []) {
  if (!incoming.length) return existing;

  const nextExisting = [...existing];
  for (const message of incoming) {
    const optimisticIndex = nextExisting.findIndex(
      (candidate) =>
        candidate.optimistic &&
        candidate.role === message.role &&
        candidate.content === message.content
    );

    if (optimisticIndex >= 0) {
      nextExisting.splice(optimisticIndex, 1);
    }
  }

  return [...nextExisting, ...incoming];
}

function MarkdownMessage({ content }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
        pre: ({ node, ...props }) => <pre className="md-pre" {...props} />,
        code: ({ inline, className, children, ...props }) => (
          inline ? (
            <code className={`md-inline-code ${className || ''}`.trim()} {...props}>{children}</code>
          ) : (
            <code className={className} {...props}>{children}</code>
          )
        ),
      }}
    >
      {content || ''}
    </ReactMarkdown>
  );
}

const BACKEND_CATALOG = {
  'gemini-cli': {
    models: ['gemini-3.1-pro-preview', 'gemini-3-flash-preview', 'gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.5-flash-lite'],
    efforts: [],
  },
  'claude-cli': {
    models: ['claude-sonnet-4-6', 'claude-opus-4-6', 'claude-haiku-4-5'],
    efforts: ['low', 'medium', 'high'],
  },
  'codex-cli': {
    models: ['gpt-5.4', 'gpt-5.3-codex', 'gpt-5.2-codex', 'gpt-5.2', 'gpt-5.1-codex-max', 'gpt-5.1-codex-mini'],
    efforts: ['low', 'medium', 'high', 'extra_high'],
  },
  'deepseek-api': {
    models: ['deepseek-reasoner', 'deepseek-chat'],
    efforts: [],
  },
  'openrouter-api': {
    models: ['deepseek/deepseek-v3.2-exp', 'moonshotai/kimi-k2.5', 'google/gemini-3.1-flash-lite-preview', 'anthropic/claude-sonnet-4.6', 'anthropic/claude-opus-4.6', 'anthropic/claude-opus-4.5'],
    efforts: [],
  },
};

const UI_TEXT = {
  en: {
    themeLabel: 'Theme',
    themeDark: 'Dark',
    themeBright: 'Bright',
    themeCli: 'CLI Retro',
    layoutLabel: 'Layout',
    layoutWorkspace: 'Workbench',
    layoutTelegram: 'Chat Mode',
    languageLabel: 'Language',
    languageEnglish: 'English',
    languageJapanese: '日本語',
    languageChinese: '简体中文',
    languageChineseTraditional: '繁體中文',
    languageKorean: '한국어',
    languageGerman: 'Deutsch',
    languageFrench: 'Français',
    languageRussian: 'Русский',
    languageArabic: 'العربية',
    cpu: 'CPU',
    ram: 'RAM',
    bridgeOnline: 'Bridge Online',
    bridgeOffline: 'Bridge Offline',
    refresh: 'Refresh',
    refreshTitle: 'Refresh agent list',
    reboot: 'Reboot',
    rebootTitle: 'Send /reboot through the bridge',
    activate: 'Activate',
    deactivate: 'Deactivate',
    active: 'active',
    inactive: 'inactive',
    editEmoji: 'Click to edit emoji',
    editDisplayName: 'Click to edit display name',
    unknownModel: 'unknown',
    you: 'User',
    placeholder: 'Message {name} (Enter to send, Shift+Enter for newline, paste or attach files for media)',
    attachTitle: 'Attach files',
    send: 'Send ➤',
    sending: '...',
    chats: 'Chats',
    backend: 'Backend',
    model: 'Model',
    effort: 'Effort',
    effortLow: 'Low',
    effortMedium: 'Medium',
    effortHigh: 'High',
    effortExtraHigh: 'Extra High',
    apply: 'Apply',
  },
  ja: {
    themeLabel: 'テーマ',
    themeDark: 'ダーク',
    themeBright: 'ライト',
    themeCli: 'CLI レトロ',
    layoutLabel: 'レイアウト',
    layoutWorkspace: 'ワークベンチ',
    layoutTelegram: 'チャットモード',
    languageLabel: '言語',
    languageEnglish: 'English',
    languageJapanese: '日本語',
    languageChinese: '简体中文',
    languageChineseTraditional: '繁體中文',
    languageKorean: '한국어',
    languageGerman: 'Deutsch',
    languageFrench: 'Français',
    languageRussian: 'Русский',
    languageArabic: 'العربية',
    cpu: 'CPU',
    ram: 'RAM',
    bridgeOnline: 'ブリッジ接続中',
    bridgeOffline: 'ブリッジ未接続',
    refresh: '更新',
    refreshTitle: 'エージェント一覧を更新',
    reboot: '再起動',
    rebootTitle: 'ブリッジへ /reboot を送信',
    activate: '有効化',
    deactivate: '無効化',
    active: '有効',
    inactive: '無効',
    editEmoji: '絵文字を編集',
    editDisplayName: '表示名を編集',
    unknownModel: '不明',
    you: 'ユーザー',
    placeholder: '{name} にメッセージ（Enter送信 / Shift+Enter改行 / 画像貼付・添付対応）',
    attachTitle: 'ファイルを添付',
    send: '送信 ➤',
    sending: '...',
    chats: 'チャット',
    backend: 'Backend',
    model: 'モデル',
    effort: '推論',
    effortLow: '低',
    effortMedium: '中',
    effortHigh: '高',
    effortExtraHigh: '最高',
    apply: '適用',
  },
  zh: {
    themeLabel: '主题',
    themeDark: '深色',
    themeBright: '浅色',
    themeCli: 'CLI 复古',
    layoutLabel: '布局',
    layoutWorkspace: '工作台',
    layoutTelegram: '聊天模式',
    languageLabel: '语言',
    languageEnglish: 'English',
    languageJapanese: '日本語',
    languageChinese: '简体中文',
    languageChineseTraditional: '繁體中文',
    languageKorean: '한국어',
    languageGerman: 'Deutsch',
    languageFrench: 'Français',
    languageRussian: 'Русский',
    languageArabic: 'العربية',
    cpu: 'CPU',
    ram: '内存',
    bridgeOnline: 'Bridge 在线',
    bridgeOffline: 'Bridge 离线',
    refresh: '刷新',
    refreshTitle: '刷新代理列表',
    reboot: '重启',
    rebootTitle: '通过 Bridge 发送 /reboot',
    activate: '启用',
    deactivate: '停用',
    active: '启用中',
    inactive: '已停用',
    editEmoji: '点击编辑 emoji',
    editDisplayName: '点击编辑显示名称',
    unknownModel: '未知',
    you: '用户',
    placeholder: '给 {name} 发消息（Enter 发送，Shift+Enter 换行，支持粘贴/附件）',
    attachTitle: '添加文件',
    send: '发送 ➤',
    sending: '...',
    chats: '聊天',
    backend: '后端',
    model: '模型',
    effort: '推理级别',
    effortLow: '低',
    effortMedium: '中',
    effortHigh: '高',
    effortExtraHigh: '最高',
    apply: '应用',
  },
  'zh-tw': {
    themeLabel: '主題',
    themeDark: '深色',
    themeBright: '淺色',
    themeCli: 'CLI 復古',
    layoutLabel: '版面',
    layoutWorkspace: '工作台',
    layoutTelegram: '聊天模式',
    languageLabel: '語言',
    languageEnglish: 'English',
    languageJapanese: '日本語',
    languageChinese: '简体中文',
    languageChineseTraditional: '繁體中文',
    languageKorean: '한국어',
    languageGerman: 'Deutsch',
    languageFrench: 'Français',
    languageRussian: 'Русский',
    languageArabic: 'العربية',
    cpu: 'CPU',
    ram: '記憶體',
    bridgeOnline: 'Bridge 上線',
    bridgeOffline: 'Bridge 離線',
    refresh: '重新整理',
    refreshTitle: '重新整理代理列表',
    reboot: '重啟',
    rebootTitle: '透過 Bridge 發送 /reboot',
    activate: '啟用',
    deactivate: '停用',
    active: '啟用中',
    inactive: '已停用',
    editEmoji: '點擊編輯 emoji',
    editDisplayName: '點擊編輯顯示名稱',
    unknownModel: '未知',
    you: '使用者',
    placeholder: '給 {name} 發訊息（Enter 發送，Shift+Enter 換行，支援貼上/附件）',
    attachTitle: '新增檔案',
    send: '發送 ➤',
    sending: '...',
    chats: '聊天',
    backend: '後端',
    model: '模型',
    effort: '推理級別',
    effortLow: '低',
    effortMedium: '中',
    effortHigh: '高',
    effortExtraHigh: '最高',
    apply: '套用',
  },
  ko: {
    themeLabel: '테마',
    themeDark: '다크',
    themeBright: '라이트',
    themeCli: 'CLI 레트로',
    layoutLabel: '레이아웃',
    layoutWorkspace: '워크벤치',
    layoutTelegram: '채팅 모드',
    languageLabel: '언어',
    languageEnglish: 'English',
    languageJapanese: '日本語',
    languageChinese: '简体中文',
    languageChineseTraditional: '繁體中文',
    languageKorean: '한국어',
    languageGerman: 'Deutsch',
    languageFrench: 'Français',
    languageRussian: 'Русский',
    languageArabic: 'العربية',
    cpu: 'CPU',
    ram: 'RAM',
    bridgeOnline: '브리지 온라인',
    bridgeOffline: '브리지 오프라인',
    refresh: '새로고침',
    refreshTitle: '에이전트 목록 새로고침',
    reboot: '재시작',
    rebootTitle: '브리지를 통해 /reboot 전송',
    activate: '활성화',
    deactivate: '비활성화',
    active: '활성',
    inactive: '비활성',
    editEmoji: '이모지 편집',
    editDisplayName: '표시 이름 편집',
    unknownModel: '알 수 없음',
    you: '사용자',
    placeholder: '{name}에게 메시지 (Enter 전송, Shift+Enter 줄바꿈, 파일 첨부 가능)',
    attachTitle: '파일 첨부',
    send: '전송 ➤',
    sending: '...',
    chats: '채팅',
    backend: '백엔드',
    model: '모델',
    effort: '추론 수준',
    effortLow: '낮음',
    effortMedium: '보통',
    effortHigh: '높음',
    effortExtraHigh: '최고',
    apply: '적용',
  },
  de: {
    themeLabel: 'Design',
    themeDark: 'Dunkel',
    themeBright: 'Hell',
    themeCli: 'CLI Retro',
    layoutLabel: 'Layout',
    layoutWorkspace: 'Workbench',
    layoutTelegram: 'Chat-Modus',
    languageLabel: 'Sprache',
    languageEnglish: 'English',
    languageJapanese: '日本語',
    languageChinese: '简体中文',
    languageChineseTraditional: '繁體中文',
    languageKorean: '한국어',
    languageGerman: 'Deutsch',
    languageFrench: 'Français',
    languageRussian: 'Русский',
    languageArabic: 'العربية',
    cpu: 'CPU',
    ram: 'RAM',
    bridgeOnline: 'Bridge online',
    bridgeOffline: 'Bridge offline',
    refresh: 'Aktualisieren',
    refreshTitle: 'Agentenliste aktualisieren',
    reboot: 'Neustart',
    rebootTitle: '/reboot über Bridge senden',
    activate: 'Aktivieren',
    deactivate: 'Deaktivieren',
    active: 'aktiv',
    inactive: 'inaktiv',
    editEmoji: 'Emoji bearbeiten',
    editDisplayName: 'Anzeigenamen bearbeiten',
    unknownModel: 'unbekannt',
    you: 'Benutzer',
    placeholder: 'Nachricht an {name} (Enter senden, Shift+Enter neue Zeile, Dateien einfügen/anhängen)',
    attachTitle: 'Dateien anhängen',
    send: 'Senden ➤',
    sending: '...',
    chats: 'Chats',
    backend: 'Backend',
    model: 'Modell',
    effort: 'Denktiefe',
    effortLow: 'Niedrig',
    effortMedium: 'Mittel',
    effortHigh: 'Hoch',
    effortExtraHigh: 'Sehr hoch',
    apply: 'Anwenden',
  },
  fr: {
    themeLabel: 'Thème',
    themeDark: 'Sombre',
    themeBright: 'Clair',
    themeCli: 'CLI Rétro',
    layoutLabel: 'Disposition',
    layoutWorkspace: 'Workbench',
    layoutTelegram: 'Mode Chat',
    languageLabel: 'Langue',
    languageEnglish: 'English',
    languageJapanese: '日本語',
    languageChinese: '简体中文',
    languageChineseTraditional: '繁體中文',
    languageKorean: '한국어',
    languageGerman: 'Deutsch',
    languageFrench: 'Français',
    languageRussian: 'Русский',
    languageArabic: 'العربية',
    cpu: 'CPU',
    ram: 'RAM',
    bridgeOnline: 'Bridge en ligne',
    bridgeOffline: 'Bridge hors ligne',
    refresh: 'Actualiser',
    refreshTitle: "Actualiser la liste d'agents",
    reboot: 'Redémarrer',
    rebootTitle: 'Envoyer /reboot via le Bridge',
    activate: 'Activer',
    deactivate: 'Désactiver',
    active: 'actif',
    inactive: 'inactif',
    editEmoji: "Cliquer pour modifier l'emoji",
    editDisplayName: "Cliquer pour modifier le nom d'affichage",
    unknownModel: 'inconnu',
    you: 'Vous',
    placeholder: 'Message à {name} (Entrée pour envoyer, Maj+Entrée pour retour à la ligne)',
    attachTitle: 'Joindre des fichiers',
    send: 'Envoyer ➤',
    sending: '...',
    chats: 'Discussions',
    backend: 'Backend',
    model: 'Modèle',
    effort: 'Réflexion',
    effortLow: 'Faible',
    effortMedium: 'Moyen',
    effortHigh: 'Élevé',
    effortExtraHigh: 'Très élevé',
    apply: 'Appliquer',
  },
  ru: {
    themeLabel: 'Тема',
    themeDark: 'Тёмная',
    themeBright: 'Светлая',
    themeCli: 'CLI Ретро',
    layoutLabel: 'Макет',
    layoutWorkspace: 'Workbench',
    layoutTelegram: 'Режим чата',
    languageLabel: 'Язык',
    languageEnglish: 'English',
    languageJapanese: '日本語',
    languageChinese: '简体中文',
    languageChineseTraditional: '繁體中文',
    languageKorean: '한국어',
    languageGerman: 'Deutsch',
    languageFrench: 'Français',
    languageRussian: 'Русский',
    languageArabic: 'العربية',
    cpu: 'ЦП',
    ram: 'ОЗУ',
    bridgeOnline: 'Мост онлайн',
    bridgeOffline: 'Мост офлайн',
    refresh: 'Обновить',
    refreshTitle: 'Обновить список агентов',
    reboot: 'Перезапуск',
    rebootTitle: 'Отправить /reboot через мост',
    activate: 'Включить',
    deactivate: 'Отключить',
    active: 'активен',
    inactive: 'неактивен',
    editEmoji: 'Нажмите для редактирования emoji',
    editDisplayName: 'Нажмите для редактирования имени',
    unknownModel: 'неизвестно',
    you: 'Пользователь',
    placeholder: 'Сообщение для {name} (Enter — отправить, Shift+Enter — новая строка)',
    attachTitle: 'Прикрепить файлы',
    send: 'Отправить ➤',
    sending: '...',
    chats: 'Чаты',
    backend: 'Бэкенд',
    model: 'Модель',
    effort: 'Уровень рассуждений',
    effortLow: 'Низкий',
    effortMedium: 'Средний',
    effortHigh: 'Высокий',
    effortExtraHigh: 'Очень высокий',
    apply: 'Применить',
  },
  ar: {
    themeLabel: 'السمة',
    themeDark: 'داكن',
    themeBright: 'فاتح',
    themeCli: 'CLI كلاسيكي',
    layoutLabel: 'التخطيط',
    layoutWorkspace: 'لوحة العمل',
    layoutTelegram: 'وضع الدردشة',
    languageLabel: 'اللغة',
    languageEnglish: 'English',
    languageJapanese: '日本語',
    languageChinese: '简体中文',
    languageChineseTraditional: '繁體中文',
    languageKorean: '한국어',
    languageGerman: 'Deutsch',
    languageFrench: 'Français',
    languageRussian: 'Русский',
    languageArabic: 'العربية',
    cpu: 'المعالج',
    ram: 'الذاكرة',
    bridgeOnline: 'الجسر متصل',
    bridgeOffline: 'الجسر غير متصل',
    refresh: 'تحديث',
    refreshTitle: 'تحديث قائمة العملاء',
    reboot: 'إعادة تشغيل',
    rebootTitle: 'إرسال /reboot عبر الجسر',
    activate: 'تفعيل',
    deactivate: 'تعطيل',
    active: 'نشط',
    inactive: 'غير نشط',
    editEmoji: 'انقر لتعديل الرمز التعبيري',
    editDisplayName: 'انقر لتعديل اسم العرض',
    unknownModel: 'غير معروف',
    you: 'أنت',
    placeholder: 'رسالة إلى {name} (Enter للإرسال، Shift+Enter لسطر جديد)',
    attachTitle: 'إرفاق ملفات',
    send: 'إرسال ➤',
    sending: '...',
    chats: 'المحادثات',
    backend: 'الخلفية',
    model: 'النموذج',
    effort: 'مستوى التفكير',
    effortLow: 'منخفض',
    effortMedium: 'متوسط',
    effortHigh: 'عالٍ',
    effortExtraHigh: 'عالٍ جداً',
    apply: 'تطبيق',
  },
};

function playSendSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const oscillator = ctx.createOscillator();
    const gainNode = ctx.createGain();

    oscillator.connect(gainNode);
    gainNode.connect(ctx.destination);
    oscillator.frequency.setValueAtTime(880, ctx.currentTime);
    oscillator.frequency.setValueAtTime(1100, ctx.currentTime + 0.1);
    gainNode.gain.setValueAtTime(0.3, ctx.currentTime);
    gainNode.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.2);
    oscillator.start(ctx.currentTime);
    oscillator.stop(ctx.currentTime + 0.2);
  } catch {
    // Audio feedback is optional.
  }
}

function filePreview(file) {
  if (!file.type.startsWith('image/')) return null;
  return URL.createObjectURL(file);
}

function EditableAgentIdentity({ agent, onSave, ui, compact = false }) {
  const [editingName, setEditingName] = useState(false);
  const [draftName, setDraftName] = useState(agent.displayName || agent.name);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraftName(agent.displayName || agent.name);
  }, [agent.displayName, agent.name]);

  const save = async (fields) => {
    setSaving(true);
    try {
      await onSave(agent.id, fields);
    } finally {
      setSaving(false);
    }
  };

  const saveName = async () => {
    const nextName = draftName.trim() || agent.name;
    setEditingName(false);
    if (nextName !== (agent.displayName || agent.name)) {
      await save({ display_name: nextName });
    }
  };

  return (
    <span className={`identity-editor ${compact ? 'compact' : ''}`}>
      {editingName ? (
        <input
          className="name-edit"
          value={draftName}
          onChange={(e) => setDraftName(e.target.value)}
          onBlur={saveName}
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => {
            if (e.key === 'Enter') saveName();
            if (e.key === 'Escape') {
              setDraftName(agent.displayName || agent.name);
              setEditingName(false);
            }
          }}
          autoFocus
        />
      ) : (
        <button
          type="button"
          className="agent-name-button"
          title={ui.editDisplayName}
          disabled={saving}
          onClick={(e) => {
            e.stopPropagation();
            setEditingName(true);
          }}
        >
          {agent.displayName || agent.name}
        </button>
      )}
    </span>
  );
}

function AgentPanel({ agent, session, state, onSend, onSaveIdentity, onRunCommand, commands, ui }) {
  const displayName = agent.displayName || agent.name;
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState([]);
  const [sending, setSending] = useState(false);
  const [sendSuccess, setSendSuccess] = useState(false);
  const chatRef = useRef(null);
  const fileInputRef = useRef(null);
  const attachmentsRef = useRef([]);
  const [isApplying, setIsApplying] = useState(false);
  const [selectedBackend, setSelectedBackend] = useState(agent.activeBackend || agent.engine || '');
  const [selectedModel, setSelectedModel] = useState(session?.model || agent.model || 'default');

  useEffect(() => {
    if (chatRef.current) chatRef.current.scrollTop = chatRef.current.scrollHeight;
  }, [state.messages, state.isTyping]);

  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);

  useEffect(() => () => {
    for (const attachment of attachmentsRef.current) {
      if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
    }
  }, []);

  const updateAttachments = (files) => {
    const next = [];
    for (const file of files) {
      next.push({
        id: `${file.name}-${file.lastModified}-${Math.random().toString(36).slice(2, 8)}`,
        file,
        previewUrl: filePreview(file),
      });
    }
    setAttachments((prev) => [...prev, ...next]);
  };

  const removeAttachment = (id) => {
    setAttachments((prev) => {
      const removed = prev.find((attachment) => attachment.id === id);
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
      return prev.filter((attachment) => attachment.id !== id);
    });
  };

  const send = async () => {
    const text = input.trim();
    if (!text && !attachments.length) return;
    if (sending) return;

    const currentText = text;
    const currentAttachments = attachments;
    setInput('');
    setAttachments([]);
    setSending(true);

    try {
      await onSend(agent, currentText, currentAttachments.map((attachment) => attachment.file));
      setSendSuccess(true);
      playSendSound();
      setTimeout(() => setSendSuccess(false), 150);
      currentAttachments.forEach((attachment) => {
        if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
      });
    } catch (error) {
      console.error('Send failed:', error);
      setInput(currentText);
      setAttachments(currentAttachments);
    } finally {
      setSending(false);
    }
  };

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const onPaste = (e) => {
    const items = e.clipboardData?.items || [];
    const files = [];
    for (const item of items) {
      const file = item.getAsFile?.();
      if (file) files.push(file);
    }
    if (files.length) {
      updateAttachments(files);
      e.preventDefault();
    }
  };

  useEffect(() => {
    setSelectedBackend(session?.engine || agent.activeBackend || agent.engine || '');
    setSelectedModel(session?.model || agent.model || 'default');
  }, [session?.engine, session?.model, agent.activeBackend, agent.engine, agent.model]);

  const availableCommands = commands || [];
  const backendOptions = Array.from(new Set([
    ...(agent.allowedBackends || []).map((b) => b.engine).filter(Boolean),
    session?.engine,
    agent.activeBackend,
    agent.engine,
  ].filter(Boolean)));
  const supportsBackend = availableCommands.includes('backend');
  const showBackend = backendOptions.length > 0 || Boolean(agent.activeBackend || agent.engine);
  const supportsModel = availableCommands.includes('model') || agent.type === 'flex';

  const activeBackendKey = selectedBackend || session?.engine || agent.activeBackend || agent.engine;
  const catalogEntry = BACKEND_CATALOG[activeBackendKey] || { models: [], efforts: [] };

  const modelOptions = [...(catalogEntry.models || [])];

  const effortOptions = catalogEntry.efforts || [];
  const supportsEffort = (availableCommands.includes('effort') || agent.type === 'flex') && effortOptions.length > 0;

  useEffect(() => {
    const preferred = (agent.allowedBackends || []).find((b) => b.engine === activeBackendKey)?.model;
    const fallback = (catalogEntry.models || [])[0];
    let nextModel = preferred && modelOptions.includes(preferred) ? preferred : fallback;
    if (!nextModel && modelOptions.length > 0) nextModel = modelOptions[0];
    if (nextModel && selectedModel !== nextModel) {
      setSelectedModel(nextModel);
    }
  }, [activeBackendKey, selectedModel, modelOptions, agent.allowedBackends, catalogEntry.models]);

  const runCommand = async (cmd) => {
    if (!onRunCommand) return;
    setIsApplying(true);
    try {
      await onRunCommand(agent.id, cmd);
    } finally {
      setIsApplying(false);
    }
  };

  const onBackendChange = async (value) => {
    setSelectedBackend(value);
    const preferred = (agent.allowedBackends || []).find((b) => b.engine === value)?.model;
    const firstCatalogModel = (BACKEND_CATALOG[value]?.models || [])[0];
    const nextModel = preferred || firstCatalogModel || '';
    if (nextModel) setSelectedModel(nextModel);
    if (!supportsBackend || !value || value === (session?.engine || agent.activeBackend || agent.engine)) return;
    if (nextModel) {
      await runCommand(`/backend ${value} ${nextModel}`);
    }
  };

  const onModelChange = async (value) => {
    setSelectedModel(value);
    if (!supportsModel || !value || value === (session?.model || agent.model)) return;
    const currentEngine = session?.engine || agent.activeBackend || agent.engine;
    if (supportsBackend && selectedBackend && selectedBackend !== currentEngine) {
      await runCommand(`/backend ${selectedBackend} ${value}`);
      return;
    }
    await runCommand(`/model ${value}`);
  };

  return (
    <section className="panel">
      <div className="panel-header">
        <div className="title">
          <span className={`status-dot ${agent.online ? 'green' : 'red'}`} />
          <EditableAgentIdentity agent={agent} onSave={onSaveIdentity} ui={ui} />
          <span className="agent-id">({agent.id})</span>
        </div>

        {(showBackend || supportsModel || supportsEffort) && (
          <div className="runtime-controls">
            {showBackend && (
              <div className="runtime-control">
                <label>{ui.backend}</label>
                <select
                  value={selectedBackend}
                  disabled={isApplying || !supportsBackend}
                  onChange={(e) => onBackendChange(e.target.value)}
                >
                  {backendOptions.map((engine) => (
                    <option key={engine} value={engine}>{engine}</option>
                  ))}
                </select>
              </div>
            )}

            {supportsModel && (
              <div className="runtime-control">
                <label>{ui.model}</label>
                <select
                  value={selectedModel}
                  disabled={isApplying}
                  onChange={(e) => onModelChange(e.target.value)}
                >
                  {modelOptions.map((model) => (
                    <option key={model} value={model}>{model}</option>
                  ))}
                </select>
              </div>
            )}

            {supportsEffort && (
              <div className="runtime-control effort-control">
                <label>{ui.effort}</label>
                <div className="effort-buttons">
                  {effortOptions.map((effort) => {
                    const effortLabels = {
                      low: ui.effortLow,
                      medium: ui.effortMedium,
                      high: ui.effortHigh,
                      extra_high: ui.effortExtraHigh,
                    };
                    return (
                      <button
                        key={effort}
                        type="button"
                        disabled={isApplying}
                        onClick={() => runCommand(`/effort ${effort}`)}
                      >
                        {effortLabels[effort] || effort}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="chat" ref={chatRef}>
        {state.messages.slice(-80).map((message, idx) => (
          <div key={`${message.role}-${idx}`} className={`msg ${message.role}`}>
            <div className="msg-header">
              <b>{message.role === 'user' ? ui.you : message.role === 'thinking' ? `${displayName} 💭` : displayName}:</b>
              {message.timestamp && <span className="msg-time">{formatTime(message.timestamp)}</span>}
            </div>
            <div className="msg-content">
              <MarkdownMessage content={message.content} />
            </div>
          </div>
        ))}
        {state.isTyping && (
          <div className="msg assistant typing-indicator">
            <div className="msg-header"><b>{displayName}:</b></div>
            <div className="msg-content"><span className="typing-dots"><span /><span /><span /></span></div>
          </div>
        )}
      </div>

      {!!attachments.length && (
        <div className="attachments">
          {attachments.map((attachment) => (
            <div key={attachment.id} className="attachment-chip">
              <span>{attachment.file.name}</span>
              <button type="button" onClick={() => removeAttachment(attachment.id)}>x</button>
            </div>
          ))}
        </div>
      )}

      {!!attachments.length && attachments.some((attachment) => attachment.previewUrl) && (
        <div className="preview-grid">
          {attachments.filter((attachment) => attachment.previewUrl).map((attachment) => (
            <img key={attachment.id} src={attachment.previewUrl} alt={attachment.file.name} className="preview" />
          ))}
        </div>
      )}

      <textarea
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        disabled={sending}
        placeholder={ui.placeholder.replace('{name}', displayName)}
      />

      <div className="controls-row">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          style={{ display: 'none' }}
          onChange={(e) => {
            updateAttachments(Array.from(e.target.files || []));
            e.target.value = '';
          }}
        />
        <div className="spacer" />
        <button type="button" className="attach-btn" onClick={() => fileInputRef.current?.click()} disabled={sending} title={ui.attachTitle}>+</button>
        <button
          className={`send ${sending ? 'sending' : ''} ${sendSuccess ? 'success' : ''}`}
          onClick={send}
          disabled={sending || !agent.online}
        >
          {sending ? ui.sending : ui.send}
        </button>
      </div>
    </section>
  );
}

function SortableAgentPanel({ agent, session, state, onSend, onSaveIdentity, onRunCommand, commands, ui }) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: agent.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
    zIndex: isDragging ? 1000 : 1,
  };

  return (
    <div ref={setNodeRef} style={style} className={`sortable-wrapper ${isDragging ? 'dragging' : ''}`}>
      <div className="drag-handle" {...attributes} {...listeners}>
        <span className="drag-icon">⋮⋮</span>
      </div>
      <AgentPanel
        agent={agent}
        session={session}
        state={state}
        onSend={onSend}
        onSaveIdentity={onSaveIdentity}
        onRunCommand={onRunCommand}
        commands={commands}
        ui={ui}
      />
    </div>
  );
}

const TICKER_TEXTS = [
  '「橋」は「知」を繋ぎ、「知」は未来を拓く。',
  '"A bridge connects knowledge, and knowledge opens the future."',
  '「桥」连接知识，知识开拓未来。',
  '「橋」連接知識，知識開拓未來。',
  '「다리」는 지식을 이어주고, 지식은 미래를 열어준다。',
  '„Brücke" verbindet Wissen, und Wissen erschließt die Zukunft.',
  "« Le pont relie la connaissance, et la connaissance ouvre l'avenir. »",
  '«Мост» соединяет знания, а знания открывают будущее.',
  '.«الجسر» يربط المعرفة، والمعرفة تفتح المستقبل',
];

const STORAGE_KEY_SELECTED = 'workbench-selected';
const STORAGE_KEY_ORDER = 'workbench-order';
const STORAGE_KEY_LANG = 'workbench-lang';
const STORAGE_KEY_THEME = 'workbench-theme';
const STORAGE_KEY_LAYOUT = 'workbench-layout';
const STORAGE_KEY_TELEGRAM_ACTIVE = 'workbench-telegram-active';

export default function App() {
  const [agents, setAgents] = useState([]);
  const [selected, setSelected] = useState(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY_SELECTED);
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  const [panelOrder, setPanelOrder] = useState(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY_ORDER);
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  const [language, setLanguage] = useState(() => localStorage.getItem(STORAGE_KEY_LANG) || 'en');
  const [theme, setTheme] = useState(() => localStorage.getItem(STORAGE_KEY_THEME) || 'dark');
  const [layout, setLayout] = useState(() => localStorage.getItem(STORAGE_KEY_LAYOUT) || 'workspace');
  const [sessions, setSessions] = useState({});
  const [system, setSystem] = useState(null);
  const [stateMap, setStateMap] = useState({});
  const [commandMap, setCommandMap] = useState({});
  const [refreshing, setRefreshing] = useState(false);
  const [rebooting, setRebooting] = useState(false);
  const [activeId, setActiveId] = useState(null);
  const [telegramActiveId, setTelegramActiveId] = useState(() => localStorage.getItem(STORAGE_KEY_TELEGRAM_ACTIVE) || null);
  const pollOffsets = useRef({});

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 8 },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );

  const ui = UI_TEXT[language] || UI_TEXT.en;

  const hydrateAgents = (incomingAgents) => {
    setAgents((incomingAgents || []).map((agent) => ({
      ...agent,
      displayName: agent.display_name || agent.displayName || agent.name,
      activeBackend: agent.activeBackend || agent.active_backend || agent.engine || '',
      allowedBackends: agent.allowedBackends || agent.allowed_backends || [],
      emoji: agent.emoji || '🤖',
      isActive: agent.isActive ?? agent.is_active ?? true,
    })));
  };

  const loadAgents = async (isInitial = false) => {
    setRefreshing(true);
    try {
      const [cfg, sys, sess] = await Promise.all([
        fetch('/api/config').then((r) => r.json()),
        fetch('/api/system').then((r) => r.json()),
        fetch('/api/sessions').then((r) => r.json()),
      ]);

      const incomingAgents = cfg.agents || [];
      hydrateAgents(incomingAgents);
      setSystem(sys);
      setSessions(sess.sessions || {});

      const commandsEntries = await Promise.all(
        incomingAgents.map(async (agent) => {
          try {
            const payload = await fetch(`/api/agents/${agent.id}/commands`).then((r) => r.json());
            return [agent.id, payload.commands || []];
          } catch {
            return [agent.id, []];
          }
        })
      );
      setCommandMap(Object.fromEntries(commandsEntries));

      if (isInitial && !selected.length) {
        const onlineIds = incomingAgents.filter((a) => a.isActive !== false && a.online).map((a) => a.id);
        if (onlineIds.length > 0) {
          setSelected(onlineIds.slice(0, 9));
        } else {
          const activeIds = incomingAgents.filter((a) => a.isActive !== false).map((a) => a.id);
          if (activeIds.length > 0) setSelected(activeIds.slice(0, 9));
        }
      }

      const newStateMap = {};
      for (const agent of incomingAgents) {
        const transcript = await fetch(`/api/transcript/${agent.id}?limit=60`).then((r) => r.json());
        pollOffsets.current[agent.id] = transcript.offset || 0;
        const transcriptMessages = normalizeMessages(transcript.messages);

        // Project log: load recent project conversation history for this agent.
        // Entries are stored by the workbench server independently of HASHI transcripts.
        let projectMessages = [];
        try {
          const projectList = await fetch('/api/project-log/list').then((r) => r.json());
          for (const proj of projectList.projects || []) {
            const log = await fetch(`/api/project-log?project=${encodeURIComponent(proj.name)}&limit=100`).then((r) => r.json());
            for (const entry of log.entries || []) {
              if (entry.agent !== agent.id) continue;
              projectMessages.push({
                role: entry.direction === 'outbound' ? 'user' : 'assistant',
                content: entry.text,
                source: `project-log:${entry.project}`,
                timestamp: entry.ts,
                projectTags: {
                  project: entry.project,
                  shimanto_phases: entry.shimanto_phases,
                  nagare_workflows: entry.nagare_workflows,
                  scope: entry.scope,
                },
              });
            }
          }
        } catch { /* project log unavailable, not fatal */ }

        // Merge: project log messages as base (older), dedup with transcript by content+role
        const seen = new Set(transcriptMessages.map((m) => `${m.role}:${m.content}`));
        const uniqueProjectMessages = projectMessages.filter((m) => !seen.has(`${m.role}:${m.content}`));
        const allMessages = [...uniqueProjectMessages, ...transcriptMessages]
          .sort((a, b) => (a.timestamp || '') < (b.timestamp || '') ? -1 : 1)
          .slice(-200);

        newStateMap[agent.id] = { messages: allMessages };
      }
      setStateMap((prev) => ({ ...prev, ...newStateMap }));
    } finally {
      setRefreshing(false);
    }
  };

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY_SELECTED, JSON.stringify(selected));
  }, [selected]);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY_ORDER, JSON.stringify(panelOrder));
  }, [panelOrder]);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY_LANG, language);
  }, [language]);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY_THEME, theme);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY_LAYOUT, layout);
  }, [layout]);

  useEffect(() => {
    if (telegramActiveId) {
      localStorage.setItem(STORAGE_KEY_TELEGRAM_ACTIVE, telegramActiveId);
    }
  }, [telegramActiveId]);

  useEffect(() => {
    loadAgents(true);
  }, []);

  const handleDragStart = (event) => {
    setActiveId(event.active.id);
  };

  const handleDragEnd = (event) => {
    const { active, over } = event;
    setActiveId(null);
    if (over && active.id !== over.id) {
      setPanelOrder((currentOrder) => {
        const oldIndex = currentOrder.indexOf(active.id);
        const newIndex = currentOrder.indexOf(over.id);
        return arrayMove(currentOrder, oldIndex, newIndex);
      });
    }
  };

  useEffect(() => {
    const timer = setInterval(async () => {
      const [cfg, sys, sess] = await Promise.all([
        fetch('/api/config').then((r) => r.json()),
        fetch('/api/system').then((r) => r.json()),
        fetch('/api/sessions').then((r) => r.json()),
      ]);
      hydrateAgents(cfg.agents || []);
      setSystem(sys);
      setSessions(sess.sessions || {});
    }, 2000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!agents.length) return undefined;
    const timer = setInterval(async () => {
      for (const agent of agents) {
        const offset = pollOffsets.current[agent.id] || 0;
        const response = await fetch(`/api/transcript/${agent.id}/poll?offset=${offset}`).then((r) => r.json());
        pollOffsets.current[agent.id] = response.offset || offset;
        if (response.messages?.length) {
          const incomingMessages = normalizeMessages(response.messages);
          const hasAssistant = incomingMessages.some((m) => m.role === 'assistant');
          setStateMap((prev) => ({
            ...prev,
            [agent.id]: {
              ...prev[agent.id],
              messages: reconcileMessages(prev[agent.id]?.messages || [], incomingMessages).slice(-200),
              ...(hasAssistant ? { isTyping: false } : {}),
            },
          }));
        }
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [agents]);

  useEffect(() => {
    setPanelOrder((currentOrder) => {
      const filtered = currentOrder.filter(id => selected.includes(id));
      const newIds = selected.filter(id => !filtered.includes(id));
      return [...filtered, ...newIds].slice(0, 9);
    });
  }, [selected]);

  const selectedAgents = useMemo(() => {
    const agentMap = new Map(agents.map(a => [a.id, a]));
    const orderedIds = panelOrder.length > 0
      ? panelOrder.filter(id => selected.includes(id))
      : selected;
    return orderedIds
      .map(id => agentMap.get(id))
      .filter(Boolean)
      .slice(0, 9);
  }, [agents, selected, panelOrder]);

  const telegramAgent = useMemo(() => {
    if (!telegramActiveId) return null;
    return agents.find((agent) => agent.id === telegramActiveId) || null;
  }, [agents, telegramActiveId]);

  useEffect(() => {
    if (!agents.length) {
      setTelegramActiveId(null);
      return;
    }
    if (!telegramActiveId || !agents.some((agent) => agent.id === telegramActiveId)) {
      const preferredAgent = selectedAgents[0] || agents[0];
      if (preferredAgent) setTelegramActiveId(preferredAgent.id);
    }
  }, [agents, selectedAgents, telegramActiveId]);

  async function sendMessage(agent, text, files) {
    let optimisticMessage = null;
    if (text) {
      optimisticMessage = makeOptimisticMessage(text);
      setStateMap((prev) => {
        const current = prev[agent.id] || { messages: [] };
        return {
          ...prev,
          [agent.id]: {
            ...current,
            messages: [...(current.messages || []), optimisticMessage].slice(-200),
          },
        };
      });
    }

    const removeOptimisticMessage = () => {
      if (!optimisticMessage) return;
      setStateMap((prev) => {
        const current = prev[agent.id] || { messages: [] };
        return {
          ...prev,
          [agent.id]: {
            ...current,
            messages: (current.messages || []).filter(
              (message) => message.optimisticId !== optimisticMessage.optimisticId
            ),
          },
        };
      });
    };

    if (text && !files.length && isCommandInput(text)) {
      try {
        await runAgentCommand(agent.id, text);
        return;
      } catch (error) {
        removeOptimisticMessage();
        throw error;
      }
    }

    const form = new FormData();
    form.append('agentId', agent.id);
    if (text) {
      form.append('text', text);
      form.append('caption', text);
    }
    for (const file of files) {
      form.append('files', file, file.name);
    }

    const response = await fetch('/api/chat', {
      method: 'POST',
      body: form,
    });
    if (!response.ok) {
      const body = await response.text();
      removeOptimisticMessage();
      throw new Error(body || `HTTP ${response.status}`);
    }

    if (files.length > 0) {
      appendSystemMessage(agent.id, `Media queued (${files.length} file${files.length > 1 ? 's' : ''}).`);
    }

    if (text) {
      setStateMap((prev) => ({
        ...prev,
        [agent.id]: { ...prev[agent.id], isTyping: true },
      }));
    }
  }

  async function saveAgentIdentity(id, fields) {
    const response = await fetch(`/api/agents/${id}/metadata`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(fields),
    });
    if (!response.ok) {
      const body = await response.text();
      throw new Error(body || `HTTP ${response.status}`);
    }

    const payload = await response.json();
    const updated = payload.agent;
    if (!updated) return;

    setAgents((prev) => prev.map((agent) => (
      agent.id === id
        ? {
            ...agent,
            displayName: updated.displayName || updated.name,
            emoji: updated.emoji || '🤖',
          }
        : agent
    )));
  }

  async function toggleAgentActive(id, isActive) {
    const response = await fetch(`/api/agents/${id}/active`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_active: isActive }),
    });
    if (!response.ok) {
      const body = await response.text();
      throw new Error(body || `HTTP ${response.status}`);
    }
    await loadAgents(false);
    if (!isActive) {
      setSelected((prev) => prev.filter((agentId) => agentId !== id));
    }
  }

  function appendSystemMessage(agentId, text) {
    setStateMap((prev) => {
      const current = prev[agentId] || { messages: [] };
      const messages = [...(current.messages || [])];
      messages.push({ role: 'assistant', content: `System: ${text}`, source: 'system', timestamp: new Date().toISOString() });
      return { ...prev, [agentId]: { ...current, messages } };
    });
  }

  function appendCommandMessages(agentId, messages = []) {
    const renderedMessages = (messages || [])
      .map((message) => {
        if (!message?.text) return null;
        return {
          role: 'assistant',
          content: message.text,
          source: message.channel || 'system',
          timestamp: new Date().toISOString(),
        };
      })
      .filter(Boolean);

    if (!renderedMessages.length) return false;

    setStateMap((prev) => {
      const current = prev[agentId] || { messages: [] };
      return {
        ...prev,
        [agentId]: {
          ...current,
          messages: [...(current.messages || []), ...renderedMessages].slice(-200),
        },
      };
    });
    return true;
  }

  async function runAgentCommand(agentId, command) {
    const response = await fetch(`/api/agents/${agentId}/command`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload?.ok === false) {
      const err = payload?.error || `Command failed (${response.status})`;
      appendSystemMessage(agentId, `Command failed: ${command} (${err})`);
      throw new Error(err);
    }

    const cmdResult = payload?.result ?? payload ?? {};
    await loadAgents(false);
    const rendered = appendCommandMessages(
      agentId,
      cmdResult?.messages || cmdResult?.result?.messages || []
    );
    if (!rendered) {
      const commandMessage = cmdResult?.message || cmdResult?.result?.message || cmdResult?.raw || `${command} applied`;
      appendSystemMessage(agentId, commandMessage);
    }
  }

  function getBridgeCommandAgentId() {
    const preferredIds = [
      telegramActiveId,
      ...selectedAgents.map((agent) => agent.id),
      ...agents.filter((agent) => agent.isActive).map((agent) => agent.id),
      ...agents.map((agent) => agent.id),
    ].filter(Boolean);
    return preferredIds[0] || null;
  }

  async function handleReboot() {
    const agentId = getBridgeCommandAgentId();
    if (!agentId) return;
    setRebooting(true);
    try {
      await runAgentCommand(agentId, '/reboot');
    } finally {
      setRebooting(false);
    }
  }

  return (
    <div className={`app theme-${theme}${language === 'ar' ? ' lang-ar' : ''}`} dir={language === 'ar' ? 'rtl' : 'ltr'}>
      <header>
        <div className="header-brand">
          <div className="header-logo">
            <span className="logo-text">HASHI</span>
            <span className="logo-kanji">ハシ 橋</span>
          </div>
          <div className="tagline-jp">「橋」は「知」を繋ぎ、「知」は<span className="highlight">未来</span>を拓く。</div>
        </div>
        <div className="header-right">
          <div className="metrics">
            <span className="metric-chip theme-chip">
              <label htmlFor="theme-select">{ui.themeLabel}</label>
              <select
                id="theme-select"
                className="theme-select"
                value={theme}
                onChange={(e) => setTheme(e.target.value)}
              >
                <option value="dark">{ui.themeDark}</option>
                <option value="bright">{ui.themeBright}</option>
                <option value="cli">{ui.themeCli}</option>
              </select>
            </span>
            <span className="metric-chip theme-chip">
              <label htmlFor="layout-select">{ui.layoutLabel}</label>
              <select
                id="layout-select"
                className="theme-select"
                value={layout}
                onChange={(e) => setLayout(e.target.value)}
              >
                <option value="workspace">{ui.layoutWorkspace}</option>
                <option value="telegram">{ui.layoutTelegram}</option>
              </select>
            </span>
            <span className="metric-chip language-chip">
              <label htmlFor="lang-select">🌐</label>
              <select
                id="lang-select"
                className="lang-select"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
              >
                <option value="en">{ui.languageEnglish}</option>
                <option value="ja">{ui.languageJapanese}</option>
                <option value="zh">{ui.languageChinese}</option>
                <option value="zh-tw">{ui.languageChineseTraditional}</option>
                <option value="ko">{ui.languageKorean}</option>
                <option value="de">{ui.languageGerman}</option>
                <option value="fr">{ui.languageFrench}</option>
                <option value="ru">{ui.languageRussian}</option>
                <option value="ar">{ui.languageArabic}</option>
              </select>
            </span>
            <span className="metric-chip">{ui.cpu}: {system?.cpuPercent ?? '--'}%</span>
            <span className="metric-chip">{ui.ram}: {system ? `${system.ramUsedGb}/${system.ramTotalGb} GB` : '--'}</span>
            <span className="metric-chip metric-dot-only" title={system?.bridge?.online ? ui.bridgeOnline : ui.bridgeOffline}>
              <span className={`status-dot ${system?.bridge?.online ? 'green' : 'red'}`} />
            </span>
          </div>
          <div className="copyright">© 2026 Barry Li · <a href="https://github.com/Bazza1982/hashi" target="_blank" rel="noopener noreferrer" className="license-link">MIT License</a></div>
        </div>
      </header>

      {layout !== 'telegram' ? (
        <div className="selector">
          <button
            className={`refresh-btn reboot-btn ${rebooting ? 'refreshing' : ''}`}
            onClick={handleReboot}
            disabled={rebooting || refreshing || !agents.length}
            title={ui.rebootTitle}
          >
            {rebooting ? '...' : ui.reboot}
          </button>
          <button
            className={`refresh-btn ${refreshing ? 'refreshing' : ''}`}
            onClick={() => loadAgents(false)}
            disabled={refreshing}
            title={ui.refreshTitle}
          >
            {refreshing ? '...' : ui.refresh}
          </button>
          {agents.map((agent) => {
            const checked = selected.includes(agent.id);
            return (
              <div key={agent.id} className={`agent-selector ${agent.isActive ? '' : 'inactive'}`}>
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={!agent.isActive}
                  onChange={(e) => {
                    setSelected((prev) => {
                      if (e.target.checked) return [...new Set([...prev, agent.id])].slice(0, 9);
                      return prev.filter((id) => id !== agent.id);
                    });
                  }}
                />
                <EditableAgentIdentity agent={agent} onSave={saveAgentIdentity} ui={ui} compact />
                <span className="agent-state-label">{agent.isActive ? ui.active : ui.inactive}</span>
                <span className="agent-status">
                  <span className={`status-dot ${agent.isActive ? (agent.online ? 'green' : 'yellow') : 'red'}`} />
                </span>
                <button
                  type="button"
                  className={`agent-toggle-btn ${agent.isActive ? 'deactivate' : 'activate'}`}
                  onClick={() => toggleAgentActive(agent.id, !agent.isActive)}
                >
                  {agent.isActive ? ui.deactivate : ui.activate}
                </button>
              </div>
            );
          })}
        </div>
      ) : null}

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
      >
        {layout === 'telegram' ? (
          <main className="telegram-layout">
            <aside className="telegram-sidebar">
              <div className="telegram-sidebar-header">
                <span>{ui.chats}</span>
                <div className="telegram-sidebar-actions">
                  <button
                    className={`refresh-btn reboot-btn telegram-refresh-btn ${rebooting ? 'refreshing' : ''}`}
                    onClick={handleReboot}
                    disabled={rebooting || refreshing || !agents.length}
                    title={ui.rebootTitle}
                  >
                    {rebooting ? '...' : ui.reboot}
                  </button>
                  <button
                    className={`refresh-btn telegram-refresh-btn ${refreshing ? 'refreshing' : ''}`}
                    onClick={() => loadAgents(false)}
                    disabled={refreshing}
                    title={ui.refreshTitle}
                  >
                    {refreshing ? '...' : ui.refresh}
                  </button>
                </div>
              </div>
              <div className="telegram-chat-list">
                {agents.map((agent) => {
                  const session = sessions[agent.id];
                  const lastMessage = (stateMap[agent.id]?.messages || []).slice(-1)[0];
                  const isCurrent = telegramActiveId === agent.id;
                  const checked = selected.includes(agent.id);
                  return (
                    <div
                      key={agent.id}
                      className={`telegram-chat-item ${isCurrent ? 'active' : ''} ${agent.isActive ? '' : 'inactive'}`}
                    >
                      <button
                        type="button"
                        className="telegram-chat-select"
                        onClick={() => setTelegramActiveId(agent.id)}
                      >
                        <div className="telegram-chat-item-top">
                          <span className="telegram-chat-name">{agent.displayName || agent.name}</span>
                          <span className={`status-dot ${agent.isActive ? (agent.online ? 'green' : 'yellow') : 'red'}`} />
                        </div>
                        <div className="telegram-chat-meta">
                          {agent.engine} | {session?.model || agent.model || ui.unknownModel}
                        </div>
                        <div className="telegram-chat-preview">
                          {lastMessage?.content || agent.id}
                        </div>
                      </button>
                      <div className="telegram-chat-controls">
                        <label className="telegram-chat-check">
                          <input
                            type="checkbox"
                            checked={checked}
                            disabled={!agent.isActive}
                            onChange={(e) => {
                              setSelected((prev) => {
                                if (e.target.checked) return [...new Set([...prev, agent.id])].slice(0, 9);
                                return prev.filter((id) => id !== agent.id);
                              });
                            }}
                          />
                          <span>{agent.isActive ? ui.active : ui.inactive}</span>
                        </label>
                        <button
                          type="button"
                          className={`agent-toggle-btn ${agent.isActive ? 'deactivate' : 'activate'}`}
                          onClick={() => toggleAgentActive(agent.id, !agent.isActive)}
                        >
                          {agent.isActive ? ui.deactivate : ui.activate}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </aside>
            <section className="telegram-main">
              {telegramAgent ? (
                <AgentPanel
                  key={telegramAgent.id}
                  agent={telegramAgent}
                  session={sessions[telegramAgent.id]}
                  state={stateMap[telegramAgent.id] || { messages: [] }}
                  onSend={sendMessage}
                  onSaveIdentity={saveAgentIdentity}
                  onRunCommand={runAgentCommand}
                  commands={commandMap[telegramAgent.id] || []}
                  ui={ui}
                />
              ) : null}
            </section>
          </main>
        ) : (
          <SortableContext items={selectedAgents.map(a => a.id)} strategy={rectSortingStrategy}>
            <main className={`grid ${gridClass(selectedAgents.length)}`}>
              {selectedAgents.map((agent) => (
                <SortableAgentPanel
                  key={agent.id}
                  agent={agent}
                  session={sessions[agent.id]}
                  state={stateMap[agent.id] || { messages: [] }}
                  onSend={sendMessage}
                  onSaveIdentity={saveAgentIdentity}
                  onRunCommand={runAgentCommand}
                  commands={commandMap[agent.id] || []}
                  ui={ui}
                />
              ))}
            </main>
          </SortableContext>
        )}
      </DndContext>

      {/* ─── Scrolling Ticker Footer ─── */}
      <div className="ticker-footer" dir="ltr">
        <div className="ticker-track">
          {[...TICKER_TEXTS, ...TICKER_TEXTS].map((text, idx) => (
            <span key={idx} className="ticker-item">
              {text}
              <span className="ticker-sep">✦</span>
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
