import telebot
import os
import json
import logging
import time
import threading
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
        return default.copy() if isinstance(default, dict) else default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(default, dict):
            changed = False
            for key, val in default.items():
                if key not in data:
                    data[key] = val
                    logger.warning(f"Добавлен ключ {key} в {filename}")
                    changed = True
            if changed:
                save_json(filename, data)
        return data
    except (json.JSONDecodeError, ValueError, IOError) as e:
        logger.warning(f"Ошибка чтения {filename}: {e}. Сбрасываю к default.")
        save_json(filename, default)
        return default.copy() if isinstance(default, dict) else default

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
DEFAULT_ALLOWED = [882100075, 230729589, 675620479, 1042967059, 6481635279, 1699807628]
DEFAULT_ADMINS  = [882100075]
DEFAULT_STATS   = {"total_clicks": 0, "unique_users": 0, "last_activity": None}
DEFAULT_KB      = {"main_menu": {}}

ALLOWED_USERS = load_json('allowed_users.json', DEFAULT_ALLOWED)
ADMIN_IDS     = load_json('admin_ids.json',     DEFAULT_ADMINS)
stats         = load_json('stats.json',          DEFAULT_STATS)
kb            = load_json('knowledge_base.json', DEFAULT_KB)

MASTER_ID        = 882100075
_seen_users      = set()
user_states      = {}
user_context     = {}   # chat_id → [путь]
album_cache      = {}
album_timers     = {}
album_lock       = threading.Lock()
user_states_lock = threading.Lock()

# Кеш поиска
_search_cache      = {}
_search_cache_time = {}
_CACHE_TTL         = 30

def _invalidate_search_cache():
    _search_cache.clear()
    _search_cache_time.clear()

# ============================================================
# ТОКЕН
# ============================================================
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    logger.error("ТОКЕН НЕ НАЙДЕН! Добавь BOT_TOKEN в .env или Railway Variables.")
    exit(1)

bot = telebot.TeleBot(TOKEN)

if not kb.get('main_menu'):
    kb = {"main_menu": {}}
    save_json('knowledge_base.json', kb)

logger.info(f"База знаний загружена. Разделов: {len(kb['main_menu'])}")

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def escape_html(text):
    """Экранирует спецсимволы для parse_mode=HTML."""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def get_allowed_set():
    return set(ALLOWED_USERS)

def check_access(user_id, chat_id):
    if user_id not in get_allowed_set():
        try:
            bot.send_message(chat_id, "🚫 Доступ закрыт.\nНапишите /id и передайте ваш ID администратору.")
        except Exception:
            pass
        logger.info(f"Отказано: ID {user_id}")
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
        bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"edit не удался: {e}")
        safe_delete(chat_id, message_id)
        try:
            bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode=parse_mode)
        except Exception as e2:
            logger.error(f"send не удался: {e2}")

# ============================================================
# РАЗБОР ПУТИ (с поддержкой кавычек)
# ============================================================

def parse_path_and_name(text):
    """
    Разбирает строку вида:
      "Отдел продаж/Возражения" "Новое возражение"
      или (без кавычек, только для однословных названий)
      /Отдел продаж/Возражения/ Новое

    Возвращает (path_parts, name) или (None, None) при ошибке.
    """
    text = text.strip()
    if text.startswith('"'):
        end_quote = text.find('"', 1)
        if end_quote == -1:
            return None, None
        path_str = text[1:end_quote].strip('/')
        rest = text[end_quote+1:].strip()
        if rest.startswith('"') and rest.endswith('"'):
            name = rest[1:-1]
        else:
            name = rest.strip('"').strip()
        path_parts = [p for p in path_str.split('/') if p.strip()]
        return path_parts, name
    else:
        # без кавычек: последнее слово — имя
        last_space = text.rfind(' ')
        if last_space == -1:
            return None, None
        path_str = text[:last_space].strip('/ ')
        name = text[last_space+1:].strip()
        path_parts = [p.strip() for p in path_str.split('/') if p.strip()]
        return path_parts, name

def parse_path_and_type(text):
    """
    Для /addcontent: разбирает "путь" тип или /путь/ тип
    Тип — всегда последнее слово (одно слово, без пробелов).
    """
    text = text.strip()
    if text.startswith('"'):
        end_quote = text.find('"', 1)
        if end_quote == -1:
            return None, None
        path_str = text[1:end_quote].strip('/')
        content_type = text[end_quote+1:].strip().strip('"')
        path_parts = [p for p in path_str.split('/') if p.strip()]
        return path_parts, content_type
    else:
        last_space = text.rfind(' ')
        if last_space == -1:
            return None, None
        path_str = text[:last_space].strip('/ ')
        content_type = text[last_space+1:].strip()
        path_parts = [p.strip() for p in path_str.split('/') if p.strip()]
        return path_parts, content_type

def get_children(path_parts):
    """
    Возвращает словарь children для категории по пути.
    Если путь пустой — возвращает main_menu.
    Если узел не найден или не является категорией — возвращает None.
    """
    if not path_parts:
        return kb['main_menu']
    node = kb['main_menu']
    for key in path_parts:
        if key in node:
            val = node[key]
            if val.get('type') == 'category':
                node = val['children']
            else:
                return None
        else:
            return None
    return node

# ============================================================
# ПОИСК С КЕШЕМ
# ============================================================

def search_kb(query):
    now = time.time()
    q = query.lower()
    if q in _search_cache and now - _search_cache_time.get(q, 0) < _CACHE_TTL:
        return _search_cache[q]

    results = []

    def traverse(node, current_path):
        for key, value in node.items():
            path = current_path + [key]
            if value.get('type') == 'category':
                traverse(value.get('children', {}), path)
            elif value.get('type') == 'content':
                data = value.get('data', '')
                score = 0
                if q in key.lower():
                    score += 2
                if isinstance(data, str) and q in data.lower():
                    score += 1
                if score > 0:
                    if isinstance(data, str):
                        safe = escape_html(data)
                        preview = safe[:100] + '...' if len(safe) > 100 else safe
                    else:
                        preview = '📎 Медиафайл'
                    results.append({'path': path, 'title': key, 'preview': preview, 'score': score})

    traverse(kb['main_menu'], [])
    results.sort(key=lambda x: x['score'], reverse=True)
    top = results[:10]
    _search_cache[q] = top
    _search_cache_time[q] = now
    logger.info(f"Поиск: {query!r} → {len(top)} результатов")
    return top

# ============================================================
# НАВИГАЦИЯ
# ============================================================

def make_back_keyboard(path_parts):
    markup = types.InlineKeyboardMarkup()
    if path_parts:
        markup.add(types.InlineKeyboardButton("← Назад", callback_data="back"))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
    return markup

def show_main_menu(chat_id, message_id=None):
    user_context[chat_id] = []
    markup = types.InlineKeyboardMarkup()
    for name in kb['main_menu'].keys():
        markup.add(types.InlineKeyboardButton(name, callback_data=f"go|{name}"))
    text = "📋 Выберите раздел:"
    if message_id:
        safe_edit(chat_id, message_id, text, markup)
    else:
        try:
            bot.send_message(chat_id, text, reply_markup=markup)
        except Exception as e:
            logger.error(f"Ошибка меню: {e}")

def send_content_node(chat_id, message_id, node, path_parts):
    content_type = node.get('content_type', 'text')
    data         = node.get('data', '')
    caption      = node.get('caption', '')
    keyboard     = make_back_keyboard(path_parts)
    safe_delete(chat_id, message_id)
    try:
        if content_type == 'text':
            bot.send_message(chat_id, data, reply_markup=keyboard)
        elif content_type == 'text_with_link':
            bot.send_message(chat_id, data, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=False)
        elif content_type == 'photo':
            if isinstance(data, list):
                media = [types.InputMediaPhoto(media=fid, caption=caption if i==0 else '', parse_mode="HTML") for i, fid in enumerate(data)]
                bot.send_media_group(chat_id, media)
                bot.send_message(chat_id, "📸 Альбом отправлен.", reply_markup=keyboard)
            else:
                bot.send_photo(chat_id, data, caption=caption, reply_markup=keyboard, parse_mode="HTML")
        elif content_type == 'document':
            bot.send_document(chat_id, data, caption=caption, reply_markup=keyboard, parse_mode="HTML")
        else:
            bot.send_message(chat_id, f"⚠️ Тип '{content_type}' не поддерживается.", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Ошибка отправки контента: {e}")
        try:
            bot.send_message(chat_id, "❌ Не удалось загрузить материал.", reply_markup=keyboard)
        except Exception:
            pass

def show_category_menu(chat_id, message_id, children, path_parts):
    user_context[chat_id] = path_parts
    if not children:
        markup = types.InlineKeyboardMarkup()
        if path_parts:
            markup.add(types.InlineKeyboardButton("← Назад", callback_data="back"))
        markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
        safe_edit(chat_id, message_id, "📭 В этом разделе пока нет материалов.", markup)
        return

    markup = types.InlineKeyboardMarkup()
    for name in children.keys():
        markup.add(types.InlineKeyboardButton(name, callback_data=f"go|{name}"))
    if path_parts:
        markup.add(types.InlineKeyboardButton("← Назад", callback_data="back"))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
    safe_edit(chat_id, message_id, "📂 Выберите подраздел:", markup)

# ============================================================
# ФИНАЛИЗАЦИЯ АЛЬБОМА
# ============================================================
def finalize_album(media_group_id, chat_id):
    with album_lock:
        if media_group_id not in album_cache:
            return
        album        = album_cache[media_group_id]
        user_id      = album["user_id"]
        path_parts   = album["path"]
        content_type = album["content_type"]
        files        = album["files"]
        caption      = album["caption"]
        del album_cache[media_group_id]
        album_timers.pop(media_group_id, None)

    parent = get_children(path_parts)
    if parent is None:
        bot.send_message(chat_id, "❌ Путь не найден при финализации альбома.")
        return

    data = files[0] if len(files) == 1 else files
    with user_states_lock:
        user_states[user_id] = {
            "step": "waiting_label", "path": path_parts,
            "content_type": content_type, "data": data,
            "caption": caption, "is_album": len(files) > 1
        }
    bot.send_message(
        chat_id,
        f"📸 {'Альбом' if len(files) > 1 else 'Фото'} собран ({len(files)} шт.).\n"
        f"Введите название кнопки:",
        parse_mode="HTML"
    )

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
        logger.error(f"Ошибка /start: {e}")
        notify_admins(f"Ошибка /start: {e}", is_error=True)


@bot.message_handler(commands=['id', 'myid'])
def cmd_id(message):
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
        "/id или /myid — ваш Телеграм ID\n"
        "/help — эта справка\n"
        "/search текст — поиск по базе знаний\n\n"
        "<b>Для администраторов:</b>\n"
        "/stats — статистика\n"
        "/listusers — список пользователей\n"
        "/adduser 123456 — добавить пользователя\n"
        "/removeuser 123456 — удалить пользователя\n"
        "/addadmin 123456 — назначить администратора\n"
        "/removeadmin 123456 — убрать права администратора\n"
        "/listtree — структура базы знаний\n\n"
        "<b>Управление базой знаний:</b>\n"
        '/addcategory "путь" "название" — создать категорию\n'
        '/addcontent "путь" тип — добавить контент\n'
        '/delete "путь" — удалить элемент\n'
        "/reloadkb — перезагрузить базу знаний\n\n"
        "<b>Примеры путей:</b>\n"
        '<code>/addcategory "Отдел продаж/Возражения" "Новый тип"</code>\n'
        '<code>/addcontent "Отдел продаж/Цены" text</code>\n'
        '<code>/delete "Отдел продаж/Бонусы"</code>'
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
        f"📋 В белом списке: {len(ALLOWED_USERS)}\n"
        f"🔍 Запросов в кеше поиска: {len(_search_cache)}"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=['listusers'])
def cmd_list_users(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    if not ALLOWED_USERS:
        bot.send_message(message.chat.id, "📭 Список пуст.")
        return
    text = "📋 <b>Разрешённые пользователи:</b>\n" + "\n".join(f"• <code>{uid}</code>" for uid in ALLOWED_USERS)
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
    notify_admins(f"➕ Добавлен {new_id} (добавил: {message.from_user.id})")


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
    notify_admins(f"➖ Удалён {rem_id} (удалил: {message.from_user.id})")


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
    bot.send_message(message.chat.id, f"✅ {new_admin} теперь администратор.")
    notify_admins(f"👑 Назначен {new_admin} (назначил: {message.from_user.id})")


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
        bot.send_message(message.chat.id, f"⚠️ {rem_id} не является администратором.")
        return
    if len(ADMIN_IDS) <= 1:
        bot.send_message(message.chat.id, "⚠️ Нельзя удалить единственного администратора.")
        return
    ADMIN_IDS.remove(rem_id)
    save_json('admin_ids.json', ADMIN_IDS)
    bot.send_message(message.chat.id, f"✅ {rem_id} больше не администратор.")
    notify_admins(f"👑 {message.from_user.id} убрал администратора {rem_id}.")


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
    text = f"📋 <b>Структура базы знаний:</b>\n<pre>{tree}</pre>"
    if len(text) > 4000:
        chunks = [tree[i:i+3800] for i in range(0, len(tree), 3800)]
        for i, chunk in enumerate(chunks):
            header = "📋 <b>Структура (продолжение):</b>\n" if i > 0 else "📋 <b>Структура базы знаний:</b>\n"
            bot.send_message(message.chat.id, header + f"<pre>{chunk}</pre>", parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=['reloadkb'])
def cmd_reload_kb(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    global kb
    path = os.path.join(DATA_DIR, 'knowledge_base.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            kb = json.load(f)
        _invalidate_search_cache()
        bot.send_message(message.chat.id, "✅ База знаний перезагружена. Кеш поиска сброшен.")
        notify_admins(f"🔄 Админ {message.from_user.id} перезагрузил базу знаний.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка перезагрузки: {e}")
        notify_admins(f"Ошибка перезагрузки: {e}", is_error=True)


@bot.message_handler(commands=['addcategory'])
def cmd_add_category(message):
    """
    Создаёт категорию.
    Синтаксис: /addcategory "путь/к/родителю" "Название"
    """
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "ℹ️ Использование:\n"
            '<code>/addcategory "путь/к/разделу" "Название"</code>\n\n'
            "Примеры:\n"
            '<code>/addcategory "Отдел продаж/Возражения" "Новый тип"</code>\n'
            '<code>/addcategory "" "Новый отдел"</code> — в корне меню',
            parse_mode="HTML"
        )
        return

    path_parts, new_name = parse_path_and_name(parts[1])
    if path_parts is None or not new_name:
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат. Используй кавычки:\n"
            '<code>/addcategory "Отдел продаж" "Новый раздел"</code>',
            parse_mode="HTML"
        )
        return

    parent = get_children(path_parts)
    if parent is None:
        bot.send_message(message.chat.id, f"❌ Путь не найден: {'/'.join(path_parts)}")
        return

    if new_name in parent:
        bot.send_message(message.chat.id, f"⚠️ Элемент '{new_name}' уже существует.")
        return

    parent[new_name] = {"type": "category", "children": {}}
    save_json('knowledge_base.json', kb)
    _invalidate_search_cache()
    location = f"/{'/'.join(path_parts)}/" if path_parts else "корень меню"
    bot.send_message(message.chat.id, f"✅ Категория <b>{escape_html(new_name)}</b> создана в {escape_html(location)}.", parse_mode="HTML")


@bot.message_handler(commands=['delete'])
def cmd_delete(message):
    """
    Удаляет элемент (категорию или контент).
    Синтаксис: /delete "путь/к/элементу"
    """
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            'ℹ️ Использование: /delete "путь/к/элементу"\n'
            'Пример: /delete "Отдел продаж/Бонусы"',
            parse_mode="HTML"
        )
        return

    raw = parts[1].strip().strip('"')
    path_parts = [p.strip() for p in raw.split('/') if p.strip()]

    if not path_parts:
        bot.send_message(message.chat.id, "❌ Нельзя удалить главное меню.")
        return

    parent = get_children(path_parts[:-1])
    if parent is None:
        bot.send_message(message.chat.id, f"❌ Путь не найден: {'/'.join(path_parts[:-1])}")
        return

    target = path_parts[-1]
    if target in parent:
        del parent[target]
        save_json('knowledge_base.json', kb)
        _invalidate_search_cache()
        bot.send_message(message.chat.id, f"✅ Элемент <b>{escape_html(target)}</b> удалён.", parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, f"❌ Элемент '{target}' не найден.")


@bot.message_handler(commands=['addcontent'])
def cmd_add_content_start(message):
    """
    Начинает процесс добавления контента.
    Синтаксис: /addcontent "путь" тип
    """
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "ℹ️ Использование:\n"
            '<code>/addcontent "путь" тип</code>\n'
            "Типы: text, photo, document, text_with_link\n\n"
            'Пример: /addcontent "Отдел продаж/Возражения" text',
            parse_mode="HTML"
        )
        return

    path_parts, content_type = parse_path_and_type(parts[1])
    if path_parts is None or not content_type:
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат. Используй кавычки:\n"
            '<code>/addcontent "Отдел продаж" text</code>',
            parse_mode="HTML"
        )
        return

    if content_type not in ('text', 'photo', 'document', 'text_with_link'):
        bot.send_message(
            message.chat.id,
            f"❌ Неизвестный тип: {content_type}\n"
            "Допустимые: text, photo, document, text_with_link"
        )
        return

    parent = get_children(path_parts)
    if parent is None:
        bot.send_message(message.chat.id, f"❌ Путь не найден: {'/'.join(path_parts)}")
        return

    with user_states_lock:
        user_states[message.from_user.id] = {
            "step": "waiting_data",
            "path": path_parts,
            "content_type": content_type
        }

    type_hints = {
        "text":          "напишите текст",
        "text_with_link":"напишите текст с HTML-ссылками: &lt;a href='url'&gt;текст&lt;/a&gt;",
        "photo":         "отправьте фото (можно альбом)",
        "document":      "отправьте файл (PDF и др.)"
    }
    bot.send_message(
        message.chat.id,
        f"✏️ Раздел: <b>/{'/'.join(path_parts)}/</b>\n"
        f"Тип: <b>{content_type}</b>\n\n"
        f"Действие: {type_hints.get(content_type, 'отправьте контент')}\n\n"
        f"Для отмены: /cancel",
        parse_mode="HTML"
    )


@bot.message_handler(commands=['cancel'])
def cmd_cancel(message):
    removed = False
    with user_states_lock:
        if message.from_user.id in user_states:
            del user_states[message.from_user.id]
            removed = True
    bot.send_message(message.chat.id, "✅ Действие отменено." if removed else "Нет активных действий.")


@bot.message_handler(commands=['search'])
def cmd_search(message):
    if not check_access(message.from_user.id, message.chat.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.send_message(message.chat.id, "🔍 Использование: /search текст\nПример: /search маткапитал")
        return

    query = args[1].strip()
    if len(query) < 3:
        bot.send_message(message.chat.id, "🔍 Минимум 3 символа.")
        return
    if len(query) > 50:
        query = query[:50]

    query_safe = escape_html(query)
    results    = search_kb(query)

    if not results:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
        bot.send_message(
            message.chat.id,
            f"🔍 По запросу <b>{query_safe}</b> ничего не найдено.",
            parse_mode="HTML", reply_markup=markup
        )
        return

    markup = types.InlineKeyboardMarkup()
    for r in results:
        cb = "go|" + r['title']
        if len(cb.encode('utf-8')) <= 64:
            markup.add(types.InlineKeyboardButton(f"📄 {r['title']}", callback_data=cb))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))

    text = f"🔍 По запросу <b>{query_safe}</b> найдено {len(results)}:\n\n"
    for r in results:
        text += f"• <b>{escape_html(r['title'])}</b>\n  {r['preview']}\n\n"

    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)

# ============================================================
# ЕДИНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ============================================================
@bot.message_handler(content_types=['text', 'photo', 'document'])
def handle_all_messages(message):
    user_id = message.from_user.id

    with user_states_lock:
        state = user_states.get(user_id)

    # Шаг 1: ввод данных для /addcontent
    if state and state.get("step") == "waiting_data":
        content_type = state.get("content_type", "text")

        if content_type == 'photo':
            if not message.photo:
                bot.send_message(message.chat.id, "❌ Отправьте фото.")
                return
            media_group_id = message.media_group_id
            if media_group_id:
                with album_lock:
                    if media_group_id not in album_cache:
                        album_cache[media_group_id] = {
                            "files": [], "user_id": user_id,
                            "path": state.get("path", []),
                            "content_type": content_type,
                            "caption": message.caption or ""
                        }
                    file_id = message.photo[-1].file_id
                    album_cache[media_group_id]["files"].append(file_id)
                    if not album_cache[media_group_id]["caption"] and message.caption:
                        album_cache[media_group_id]["caption"] = message.caption
                    is_first = len(album_cache[media_group_id]["files"]) == 1
                    if media_group_id in album_timers:
                        album_timers[media_group_id].cancel()
                    timer = threading.Timer(5.0, finalize_album, args=[media_group_id, message.chat.id])
                    timer.daemon = True
                    timer.start()
                    album_timers[media_group_id] = timer
                if is_first:
                    bot.send_message(message.chat.id, "📸 Получил первое фото. Отправьте остальные — через 5 сек обработаю.")
                return
            else:
                data = message.photo[-1].file_id
                caption = message.caption or ""
                with user_states_lock:
                    if user_id in user_states:
                        user_states[user_id].update({"step": "waiting_label", "data": data, "caption": caption})
                    else:
                        return
                bot.send_message(message.chat.id, "🔑 Введите название кнопки.\nПример: <b>Фото</b>", parse_mode="HTML")
                return

        if content_type in ('text', 'text_with_link'):
            if not message.text:
                bot.send_message(message.chat.id, "❌ Отправьте текстовое сообщение.")
                return
            data, caption = message.text, ""
        elif content_type == 'document':
            if not message.document:
                bot.send_message(message.chat.id, "❌ Отправьте документ.")
                return
            data = message.document.file_id
            caption = message.caption or ""
        else:
            bot.send_message(message.chat.id, f"❌ Неподдерживаемый тип: {content_type}")
            with user_states_lock:
                user_states.pop(user_id, None)
            return

        with user_states_lock:
            if user_id in user_states:
                user_states[user_id].update({"step": "waiting_label", "data": data, "caption": caption})
            else:
                return
        bot.send_message(message.chat.id, "🔑 Введите название кнопки.\nПример: <b>Новое возражение</b>", parse_mode="HTML")
        return

    # Шаг 2: ввод названия кнопки
    if state and state.get("step") == "waiting_label":
        if not message.text:
            bot.send_message(message.chat.id, "❌ Название должно быть текстом.")
            return

        label        = message.text.strip()
        path_parts   = state.get("path", [])
        content_type = state.get("content_type")
        data         = state.get("data")
        caption      = state.get("caption", "")

        parent = get_children(path_parts)
        if parent is None:
            bot.send_message(message.chat.id, f"❌ Путь не найден: {'/'.join(path_parts)}")
            with user_states_lock:
                user_states.pop(user_id, None)
            return

        new_key = label
        if new_key in parent:
            suffix = 1
            while f"{new_key}_{suffix}" in parent:
                suffix += 1
            new_key = f"{new_key}_{suffix}"
            bot.send_message(message.chat.id, f"⚠️ Название '{label}' занято. Использую '{new_key}'.")

        entry = {"type": "content", "content_type": content_type, "data": data}
        if caption:
            entry["caption"] = caption
        parent[new_key] = entry

        save_json('knowledge_base.json', kb)
        _invalidate_search_cache()
        with user_states_lock:
            user_states.pop(user_id, None)
        bot.send_message(message.chat.id, f"✅ Контент добавлен как '{new_key}'.")
        notify_admins(f"📄 Админ {user_id} добавил '{new_key}' в /{'/'.join(path_parts)}/")
        return

    # Обычное сообщение
    if user_id in get_allowed_set():
        if message.photo or message.document:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
            bot.send_message(message.chat.id, "📌 Используйте /start или кнопки меню.", reply_markup=markup)
            return

        if message.text and len(message.text.strip()) > 2:
            query      = message.text.strip()
            query_safe = escape_html(query)
            if len(query) > 50:
                query = query[:50]
            results = search_kb(query)
            markup = types.InlineKeyboardMarkup()
            if results:
                for r in results[:5]:
                    cb = "go|" + r['title']
                    if len(cb.encode('utf-8')) <= 64:
                        markup.add(types.InlineKeyboardButton(f"📄 {r['title']}", callback_data=cb))
                markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
                bot.send_message(
                    message.chat.id,
                    f"🔍 Нашёл по запросу <b>{query_safe}</b>:\n\n" +
                    "\n".join(f"• {escape_html(r['title'])}" for r in results[:5]),
                    parse_mode="HTML", reply_markup=markup
                )
            else:
                markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
                bot.send_message(
                    message.chat.id,
                    f"🔍 По запросу <b>{query_safe}</b> ничего не найдено.\nПопробуйте /search другое_слово",
                    parse_mode="HTML", reply_markup=markup
                )
        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
            bot.send_message(message.chat.id, "📌 Напишите слово для поиска или /start", reply_markup=markup)
    else:
        bot.send_message(message.chat.id, "🚫 Доступ закрыт. Напишите /id и передайте ваш ID администратору.")

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
            user_context[chat_id] = []
            show_main_menu(chat_id, message_id)
            update_stats(user_id)
            answer_query(call.id)
            return

        if data == "back":
            path_parts = user_context.get(chat_id, [])
            if not path_parts:
                show_main_menu(chat_id, message_id)
                answer_query(call.id)
                return
            parent_path = path_parts[:-1]
            user_context[chat_id] = parent_path
            children = get_children(parent_path)
            if children is None:
                show_main_menu(chat_id, message_id)
            else:
                show_category_menu(chat_id, message_id, children, parent_path)
            update_stats(user_id)
            answer_query(call.id)
            return

        if data.startswith("go|"):
            name = data.split("|", 1)[1]
            path_parts = user_context.get(chat_id, [])
            full_path  = path_parts + [name]

            node = kb['main_menu']
            for key in full_path:
                if key in node:
                    val = node[key]
                    if val.get('type') == 'category':
                        node = val['children']
                    elif val.get('type') == 'content':
                        send_content_node(chat_id, message_id, val, full_path)
                        user_context[chat_id] = full_path
                        update_stats(user_id)
                        answer_query(call.id)
                        return
                    else:
                        answer_query(call.id, "Неизвестный тип")
                        return
                else:
                    answer_query(call.id, "Раздел не найден")
                    return

            # Дошли до категории
            show_category_menu(chat_id, message_id, node, full_path)
            user_context[chat_id] = full_path
            update_stats(user_id)
            answer_query(call.id)
            return

        answer_query(call.id, "Неизвестная команда")

    except Exception as e:
        logger.error(f"Ошибка при кнопке '{data}': {e}")
        notify_admins(f"Ошибка при кнопке '{data}': {e}", is_error=True)
        answer_query(call.id, "Произошла ошибка, попробуйте ещё раз", alert=True)

# ============================================================
# ЗАПУСК — ИСПРАВЛЕНИЕ 409 Conflict
# ============================================================
if __name__ == "__main__":
    logger.info("🚀 Бот ДЦША запускается...")

    # Исправление 409: удаляем webhook и даём время старому инстансу завершиться
    try:
        bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook удалён, pending updates сброшены.")
    except Exception as e:
        logger.warning(f"Не удалось удалить webhook: {e}")

    time.sleep(2)

    notify_admins("✅ Бот запущен и работает.")
    logger.info("✅ Бот ДЦША запущен.")

    while True:
        try:
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=30,
                allowed_updates=["message", "callback_query"]
            )
        except Exception as e:
            msg = f"❌ Бот упал: {e}. Перезапуск через 5 секунд."
            logger.critical(msg)
            notify_admins(msg, is_error=True)
            time.sleep(5)
