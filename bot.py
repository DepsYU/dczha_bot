import telebot
import os
import json
import logging
import time
from telebot import types
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
# РАБОТА С JSON
# ============================================================
DATA_DIR = os.path.dirname(os.path.abspath(__file__))

def load_json(filename, default):
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        save_json(filename, default)
        return json.loads(json.dumps(default))
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, IOError) as e:
        logger.warning(f"Ошибка чтения {filename}: {e}. Сбрасываю к default.")
        save_json(filename, default)
        return json.loads(json.dumps(default))

def save_json(filename, data):
    path = os.path.join(DATA_DIR, filename)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения {filename}: {e}")

# ============================================================
# ЗАГРУЗКА ДАННЫХ
# ============================================================
DEFAULT_ALLOWED = [882100075, 230729589]
DEFAULT_ADMINS  = [882100075]
DEFAULT_STATS   = {"total_clicks": 0, "unique_users": 0, "last_activity": None}
DEFAULT_KB      = {"main_menu": {}}

ALLOWED_USERS = load_json('allowed_users.json', DEFAULT_ALLOWED)
ADMIN_IDS     = load_json('admin_ids.json',     DEFAULT_ADMINS)
stats         = load_json('stats.json',          DEFAULT_STATS)
kb            = load_json('knowledge_base.json', DEFAULT_KB)

# Главный администратор — нельзя удалить никакими командами
MASTER_ID = 882100075

_seen_users = set()
user_states = {}

# ============================================================
# ТОКЕН
# ============================================================
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    logger.error("=" * 50)
    logger.error("ТОКЕН НЕ НАЙДЕН!")
    logger.error("Локально: создай .env и добавь: BOT_TOKEN=твой_токен")
    logger.error("Railway: добавь переменную BOT_TOKEN в Variables.")
    logger.error("=" * 50)
    exit(1)

bot = telebot.TeleBot(TOKEN)

if not kb.get('main_menu'):
    logger.critical("knowledge_base.json пустой или повреждён!")
    exit(1)
logger.info(f"База знаний загружена. Разделов: {len(kb['main_menu'])}")

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def get_allowed_set():
    return set(ALLOWED_USERS)

def check_access(user_id, chat_id):
    if user_id not in get_allowed_set():
        try:
            bot.send_message(
                chat_id,
                "🚫 Доступ закрыт.\nНапишите /id и передайте ваш ID администратору."
            )
        except Exception:
            pass
        logger.info(f"Отказано в доступе: ID {user_id}")
        return False
    return True

def is_admin(user_id):
    return user_id in set(ADMIN_IDS)

def notify_admins(text, is_error=False):
    msg = f"🚨 {text}" if is_error else text
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, msg)
        except Exception:
            pass

def update_stats(user_id):
    global _seen_users
    if user_id not in _seen_users:
        _seen_users.add(user_id)
        stats["unique_users"] += 1
    stats["total_clicks"] += 1
    stats["last_activity"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json('stats.json', stats)

def answer_query(call_id, text=None, alert=False):
    try:
        if text:
            bot.answer_callback_query(call_id, text, show_alert=alert)
        else:
            bot.answer_callback_query(call_id)
    except Exception:
        pass

def safe_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass

def safe_edit(chat_id, message_id, text, keyboard, parse_mode=None):
    try:
        bot.edit_message_text(
            text, chat_id, message_id,
            reply_markup=keyboard,
            parse_mode=parse_mode
        )
    except Exception as e:
        logger.warning(f"edit не удался: {e}")
        safe_delete(chat_id, message_id)
        try:
            bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode=parse_mode)
        except Exception as e2:
            logger.error(f"send тоже не удался: {e2}")

# ============================================================
# НАВИГАЦИЯ
# ============================================================

def make_back_keyboard(path_parts):
    markup = types.InlineKeyboardMarkup()
    if len(path_parts) > 1:
        back_path = "main|" + "|".join(path_parts[:-1])
        markup.add(types.InlineKeyboardButton("← Назад", callback_data=back_path))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
    return markup

def show_main_menu(chat_id, message_id=None):
    markup = types.InlineKeyboardMarkup()
    for name in kb['main_menu'].keys():
        markup.add(types.InlineKeyboardButton(name, callback_data=f"main|{name}"))
    text = "📋 Выберите раздел:"
    if message_id:
        safe_edit(chat_id, message_id, text, markup)
    else:
        try:
            bot.send_message(chat_id, text, reply_markup=markup)
        except Exception as e:
            logger.error(f"Ошибка показа меню: {e}")

def get_node_by_path(path_parts):
    """Вспомогательная — идёт по дереву kb по списку ключей."""
    node = kb['main_menu']
    for key in path_parts:
        if isinstance(node, dict) and key in node:
            node = node[key]
            if isinstance(node, dict) and node.get('type') == 'category':
                node = node['children']
        else:
            return None
    return node

def send_content_node(chat_id, message_id, node, path_parts):
    """
    ИСПРАВЛЕНИЕ 1: parse_mode='HTML' ТОЛЬКО для text_with_link, photo, document.
    Для content_type='text' — БЕЗ parse_mode.
    Причина: тексты скриптов содержат → < > которые Телеграм парсит как HTML-теги
    и отказывается отправлять сообщение.
    """
    content_type = node.get('content_type', 'text')
    data         = node.get('data', '')
    caption      = node.get('caption', '')
    keyboard     = make_back_keyboard(path_parts)

    safe_delete(chat_id, message_id)

    try:
        if content_type == 'text':
            # БЕЗ parse_mode — тексты могут содержать < > →
            bot.send_message(chat_id, data, reply_markup=keyboard)

        elif content_type == 'text_with_link':
            # С HTML — здесь намеренно используются теги <a href>
            bot.send_message(
                chat_id, data,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=False
            )

        elif content_type == 'photo':
            bot.send_photo(
                chat_id, data,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        elif content_type == 'document':
            bot.send_document(
                chat_id, data,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        else:
            bot.send_message(
                chat_id,
                f"⚠️ Тип контента '{content_type}' не поддерживается.",
                reply_markup=keyboard
            )

    except Exception as e:
        logger.error(f"Ошибка отправки контента: {e}")
        try:
            bot.send_message(
                chat_id,
                "❌ Не удалось загрузить материал.\nПроверьте data в knowledge_base.json.",
                reply_markup=keyboard
            )
        except Exception:
            pass

def show_category_menu(chat_id, message_id, children, path_parts):
    markup = types.InlineKeyboardMarkup()
    for name in children.keys():
        cb = "main|" + "|".join(path_parts + [name])
        markup.add(types.InlineKeyboardButton(name, callback_data=cb))
    if len(path_parts) > 0:
        back_cb = "main|" + "|".join(path_parts[:-1]) if len(path_parts) > 1 else "main"
        markup.add(types.InlineKeyboardButton("← Назад", callback_data=back_cb))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
    safe_edit(chat_id, message_id, "📂 Выберите подраздел:", markup)

# ============================================================
# КОМАНДЫ
# ============================================================

@bot.message_handler(commands=['start', 'menu'])
def cmd_start(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if user_id in get_allowed_set() and user_id not in _seen_users:
        name = message.from_user.first_name or "Без имени"
        notify_admins(f"👤 Новый пользователь: {name} (ID: {user_id})")
    if not check_access(user_id, chat_id):
        return
    try:
        show_main_menu(chat_id)
        update_stats(user_id)
        logger.info(f"Пользователь {user_id} запустил бота")
    except Exception as e:
        logger.error(f"Ошибка в /start: {e}")
        notify_admins(f"Ошибка в /start: {e}", is_error=True)


@bot.message_handler(commands=['id'])
def cmd_id(message):
    # ИСПРАВЛЕНИЕ 4: <code> вместо backticks — правильный HTML
    bot.send_message(
        message.chat.id,
        f"🆔 Ваш Телеграм ID: <code>{message.from_user.id}</code>\n\n"
        f"Передайте его администратору для получения доступа.",
        parse_mode="HTML"
    )


@bot.message_handler(commands=['help'])
def cmd_help(message):
    text = (
        "🤖 <b>Бот-помощник ДЦША</b>\n\n"
        "<b>Команды для всех:</b>\n"
        "/start — главное меню\n"
        "/id — ваш Телеграм ID\n"
        "/help — эта справка\n\n"
        "<b>Для администраторов:</b>\n"
        "/stats — статистика\n"
        "/listusers — список пользователей\n"
        "/adduser 123456 — добавить пользователя\n"
        "/removeuser 123456 — удалить пользователя\n"
        "/addadmin 123456 — назначить администратора\n"
        "/removeadmin 123456 — убрать права администратора\n"
        "/listtree — структура базы знаний\n"
        "/addcategory /путь/ название — создать категорию\n"
        "/addcontent /путь/ тип — добавить контент\n"
        "/delete /путь — удалить элемент\n"
        "/reloadkb — перезагрузить базу знаний"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    text = (
        f"📊 <b>Статистика бота</b>\n"
        f"👥 Уникальных пользователей: {stats['unique_users']}\n"
        f"🖱 Всего нажатий: {stats['total_clicks']}\n"
        f"🕒 Последняя активность: {stats['last_activity'] or 'нет'}\n"
        f"📋 В белом списке: {len(ALLOWED_USERS)}"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=['listusers'])
def cmd_list_users(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    if not ALLOWED_USERS:
        bot.send_message(message.chat.id, "📭 Список пользователей пуст.")
        return
    text = "📋 <b>Разрешённые пользователи:</b>\n" + "\n".join(
        f"• <code>{uid}</code>" for uid in ALLOWED_USERS
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=['adduser'])
def cmd_add_user(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ Использование: /adduser 123456789")
        return
    try:
        new_id = int(args[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    if new_id in get_allowed_set():
        bot.send_message(message.chat.id, f"⚠️ Пользователь {new_id} уже в списке.")
        return
    ALLOWED_USERS.append(new_id)
    save_json('allowed_users.json', ALLOWED_USERS)
    bot.send_message(message.chat.id, f"✅ Пользователь {new_id} добавлен.")
    notify_admins(f"➕ Добавлен пользователь {new_id} (добавил: {message.from_user.id})")


@bot.message_handler(commands=['removeuser'])
def cmd_remove_user(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ Использование: /removeuser 123456789")
        return
    try:
        rem_id = int(args[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    if rem_id == MASTER_ID:
        bot.send_message(message.chat.id, "⛔ Нельзя удалить главного администратора.")
        return
    if rem_id not in get_allowed_set():
        bot.send_message(message.chat.id, f"⚠️ Пользователь {rem_id} не найден.")
        return
    ALLOWED_USERS.remove(rem_id)
    _seen_users.discard(rem_id)
    save_json('allowed_users.json', ALLOWED_USERS)
    bot.send_message(message.chat.id, f"✅ Пользователь {rem_id} удалён.")
    notify_admins(f"➖ Удалён пользователь {rem_id} (удалил: {message.from_user.id})")


@bot.message_handler(commands=['addadmin'])
def cmd_add_admin(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ Использование: /addadmin 123456789")
        return
    try:
        new_admin = int(args[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    if new_admin in set(ADMIN_IDS):
        bot.send_message(message.chat.id, f"⚠️ Пользователь {new_admin} уже администратор.")
        return
    ADMIN_IDS.append(new_admin)
    save_json('admin_ids.json', ADMIN_IDS)
    bot.send_message(message.chat.id, f"✅ Пользователь {new_admin} теперь администратор.")
    notify_admins(f"👑 Назначен администратор {new_admin} (назначил: {message.from_user.id})")


@bot.message_handler(commands=['removeadmin'])
def cmd_remove_admin(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ Использование: /removeadmin 123456789")
        return
    try:
        rem_id = int(args[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    if rem_id == MASTER_ID:
        bot.send_message(message.chat.id, "⛔ Нельзя убрать права у главного администратора.")
        return
    if rem_id == message.from_user.id:
        bot.send_message(message.chat.id, "❌ Нельзя убрать права у самого себя.")
        return
    if rem_id not in ADMIN_IDS:
        bot.send_message(message.chat.id, f"⚠️ Пользователь {rem_id} не является администратором.")
        return
    if len(ADMIN_IDS) <= 1:
        bot.send_message(message.chat.id, "⚠️ Нельзя удалить единственного администратора.")
        return
    ADMIN_IDS.remove(rem_id)
    save_json('admin_ids.json', ADMIN_IDS)
    bot.send_message(message.chat.id, f"✅ Пользователь {rem_id} больше не администратор.")
    notify_admins(f"👑 Админ {message.from_user.id} убрал администратора {rem_id}.")


@bot.message_handler(commands=['listtree'])
def cmd_list_tree(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    def traverse(node, indent=0):
        result = ""
        for key, value in node.items():
            if value.get('type') == 'category':
                result += "  " * indent + f"📁 {key}\n"
                result += traverse(value.get('children', {}), indent + 1)
            else:
                result += "  " * indent + f"📄 {key}\n"
        return result
    tree = traverse(kb['main_menu'])
    bot.send_message(
        message.chat.id,
        f"📋 <b>Структура базы знаний:</b>\n<pre>{tree}</pre>",
        parse_mode="HTML"
    )


@bot.message_handler(commands=['reloadkb'])
def cmd_reload_kb(message):
    """
    ИСПРАВЛЕНИЕ 2: используем DATA_DIR — работает и локально и на Railway.
    """
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    global kb
    path = os.path.join(DATA_DIR, 'knowledge_base.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            kb = json.load(f)
        bot.send_message(message.chat.id, "✅ База знаний перезагружена.")
        notify_admins(f"🔄 Админ {message.from_user.id} перезагрузил базу знаний.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка перезагрузки: {e}")
        notify_admins(f"Ошибка перезагрузки KB: {e}", is_error=True)


@bot.message_handler(commands=['addcategory'])
def cmd_add_category(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        bot.send_message(
            message.chat.id,
            "ℹ️ Использование: /addcategory /путь/ название\n"
            "Пример: /addcategory /Скрипт встречи/ Новый этап"
        )
        return
    path_str   = args[1].strip('/')
    new_name   = args[2].strip()
    path_parts = path_str.split('/') if path_str else []
    parent = kb['main_menu']
    for key in path_parts:
        if key in parent and parent[key].get('type') == 'category':
            parent = parent[key]['children']
        else:
            bot.send_message(message.chat.id, f"❌ Путь не найден: {key}")
            return
    if new_name in parent:
        bot.send_message(message.chat.id, f"⚠️ Элемент '{new_name}' уже существует.")
        return
    parent[new_name] = {"type": "category", "children": {}}
    save_json('knowledge_base.json', kb)
    bot.send_message(message.chat.id, f"✅ Категория '{new_name}' создана.")


@bot.message_handler(commands=['delete'])
def cmd_delete(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.send_message(
            message.chat.id,
            "ℹ️ Использование: /delete /путь\nПример: /delete /Бонусы"
        )
        return
    path_str   = args[1].strip('/')
    path_parts = path_str.split('/') if path_str else []
    if not path_parts:
        bot.send_message(message.chat.id, "❌ Нельзя удалить главное меню.")
        return
    parent = kb['main_menu']
    for key in path_parts[:-1]:
        if key in parent and parent[key].get('type') == 'category':
            parent = parent[key]['children']
        else:
            bot.send_message(message.chat.id, f"❌ Путь не найден: {key}")
            return
    target = path_parts[-1]
    if target in parent:
        del parent[target]
        save_json('knowledge_base.json', kb)
        bot.send_message(message.chat.id, f"✅ Элемент '{target}' удалён.")
    else:
        bot.send_message(message.chat.id, "❌ Элемент не найден.")


@bot.message_handler(commands=['addcontent'])
def cmd_add_content_start(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        bot.send_message(
            message.chat.id,
            "ℹ️ Использование: /addcontent /путь/ тип\n"
            "Типы: text, photo, document, text_with_link\n"
            "Пример: /addcontent /Возражения/ text"
        )
        return
    path_str     = args[1].strip('/')
    content_type = args[2].strip()
    path_parts   = path_str.split('/') if path_str else []
    parent = kb['main_menu']
    for key in path_parts:
        if key in parent and parent[key].get('type') == 'category':
            parent = parent[key]['children']
        else:
            bot.send_message(message.chat.id, f"❌ Путь не найден: {key}")
            return
    user_states[message.from_user.id] = {
        "step": "waiting_data",
        "path": path_parts,
        "content_type": content_type
    }
    type_hints = {
        "text":          "напишите текст",
        "text_with_link":"напишите текст с HTML-ссылками: &lt;a href='url'&gt;текст&lt;/a&gt;",
        "photo":         "отправьте фото",
        "document":      "отправьте файл (PDF и др.)"
    }
    hint = type_hints.get(content_type, "отправьте контент")
    bot.send_message(
        message.chat.id,
        f"✏️ Раздел: <b>/{path_str}/</b>\n"
        f"Тип: <b>{content_type}</b>\n\n"
        f"Действие: {hint}\n\n"
        f"Для отмены: /cancel",
        parse_mode="HTML"
    )


@bot.message_handler(commands=['cancel'])
def cmd_cancel(message):
    if message.from_user.id in user_states:
        del user_states[message.from_user.id]
        bot.send_message(message.chat.id, "✅ Действие отменено.")
    else:
        bot.send_message(message.chat.id, "Нет активных действий.")


# ============================================================
# ЕДИНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
#
# ИСПРАВЛЕНИЕ 3 — конфликт обработчиков:
# В pyTelegramBotAPI первый совпавший обработчик забирает сообщение.
# Нельзя иметь отдельные handle_content_input + handle_content_label + echo_all —
# handle_content_input с content_types=['text',...] перехватывает ВСЁ
# и echo_all никогда не вызывается.
# Решение: один обработчик handle_all_messages со всей логикой внутри.
# ============================================================

@bot.message_handler(content_types=['text', 'photo', 'document'])
def handle_all_messages(message):
    user_id = message.from_user.id
    state   = user_states.get(user_id)

    # --- Шаг 1: ввод данных для /addcontent ---
    if state and state.get("step") == "waiting_data":
        content_type = state.get("content_type", "text")

        if content_type in ('text', 'text_with_link'):
            if not message.text:
                bot.send_message(message.chat.id, "❌ Отправьте текстовое сообщение.")
                return
            data, caption = message.text, ""

        elif content_type == 'photo':
            if not message.photo:
                bot.send_message(message.chat.id, "❌ Отправьте фото.")
                return
            data    = message.photo[-1].file_id
            caption = message.caption or ""

        elif content_type == 'document':
            if not message.document:
                bot.send_message(message.chat.id, "❌ Отправьте документ/файл.")
                return
            data    = message.document.file_id
            caption = message.caption or ""

        else:
            bot.send_message(message.chat.id, f"❌ Неподдерживаемый тип: {content_type}")
            del user_states[user_id]
            return

        state.update({"step": "waiting_label", "data": data, "caption": caption})
        bot.send_message(
            message.chat.id,
            "🔑 Введите название кнопки для этого контента.\n"
            "Пример: <b>Новое возражение</b>",
            parse_mode="HTML"
        )
        return

    # --- Шаг 2: ввод названия кнопки ---
    if state and state.get("step") == "waiting_label":
        if not message.text:
            bot.send_message(message.chat.id, "❌ Название должно быть текстом.")
            return

        label        = message.text.strip()
        path_parts   = state.get("path", [])
        content_type = state.get("content_type")
        data         = state.get("data")
        caption      = state.get("caption", "")

        parent = kb['main_menu']
        for key in path_parts:
            if key in parent and parent[key].get('type') == 'category':
                parent = parent[key]['children']
            else:
                bot.send_message(message.chat.id, f"❌ Путь не найден: {key}")
                del user_states[user_id]
                return

        new_key = label
        if new_key in parent:
            suffix = 1
            while f"{new_key}_{suffix}" in parent:
                suffix += 1
            new_key = f"{new_key}_{suffix}"
            bot.send_message(
                message.chat.id,
                f"⚠️ Название '{label}' уже занято. Использую '{new_key}'."
            )

        entry = {"type": "content", "content_type": content_type, "data": data}
        if caption:
            entry["caption"] = caption
        parent[new_key] = entry

        save_json('knowledge_base.json', kb)
        del user_states[user_id]
        bot.send_message(message.chat.id, f"✅ Контент добавлен как '{new_key}'.")
        notify_admins(
            f"📄 Админ {user_id} добавил '{new_key}' в /{'/'.join(path_parts)}/"
        )
        return

    # --- Обычное сообщение ---
    if user_id in get_allowed_set():
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
        bot.send_message(
            message.chat.id,
            "📌 Используйте кнопки меню. Нажмите /start чтобы открыть меню.",
            reply_markup=markup
        )
    else:
        bot.send_message(
            message.chat.id,
            "🚫 Доступ закрыт. Напишите /id и передайте ваш ID администратору."
        )


# ============================================================
# ОБРАБОТКА КНОПОК
# ============================================================

@bot.callback_query_handler(func=lambda call: True)
def handle(call):
    user_id    = call.from_user.id
    chat_id    = call.message.chat.id
    message_id = call.message.message_id
    data       = call.data

    if not check_access(user_id, chat_id):
        answer_query(call.id)
        return

    logger.info(f"Кнопка '{data}' от {user_id}")

    try:
        if data == "main":
            show_main_menu(chat_id, message_id)
            update_stats(user_id)
            answer_query(call.id)
            return

        if not data.startswith("main|"):
            answer_query(call.id, "Неизвестная команда")
            return

        path_parts = data.split("|")[1:]
        node = kb['main_menu']

        for i, key in enumerate(path_parts):
            if not isinstance(node, dict) or key not in node:
                answer_query(call.id, "Раздел не найден")
                return

            current = node[key]

            if i == len(path_parts) - 1:
                if current.get('type') == 'content':
                    send_content_node(chat_id, message_id, current, path_parts)
                    update_stats(user_id)
                    answer_query(call.id)
                    return
                elif current.get('type') == 'category':
                    show_category_menu(
                        chat_id, message_id,
                        current.get('children', {}),
                        path_parts
                    )
                    update_stats(user_id)
                    answer_query(call.id)
                    return
            else:
                if current.get('type') == 'category':
                    node = current.get('children', {})
                else:
                    answer_query(call.id, "Раздел не найден")
                    return

    except Exception as e:
        logger.error(f"Ошибка при кнопке '{data}': {e}")
        notify_admins(f"Ошибка при кнопке '{data}': {e}", is_error=True)
        answer_query(call.id, "Произошла ошибка, попробуйте ещё раз", alert=True)
        return

    answer_query(call.id)


# ============================================================
# ЗАПУСК С АВТОПЕРЕЗАПУСКОМ
# ============================================================

if __name__ == "__main__":
    logger.info("🚀 Бот ДЦША запущен.")
    notify_admins("✅ Бот запущен и работает.")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            msg = f"❌ Бот упал: {e}. Перезапуск через 5 секунд."
            logger.critical(msg)
            notify_admins(msg, is_error=True)
            time.sleep(5)
