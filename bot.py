import telebot
import os
import json
import logging
import time
import threading
from telebot import types
from telebot.apihelper import ApiTelegramException
from dotenv import load_dotenv
from flask import Flask, request

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
user_context     = {}
album_cache      = {}
album_timers     = {}
album_lock       = threading.Lock()
user_states_lock = threading.Lock()

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
# ВСПОМОГАТЕЛЬНЫЕ
# ============================================================
def escape_html(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def get_allowed_set():
    return set(ALLOWED_USERS)

def check_access(user_id, chat_id):
    if user_id not in get_allowed_set():
        try:
            bot.send_message(chat_id, "🚫 Доступ закрыт.\nНапишите /id и передайте ваш ID администратору.")
        except Exception:
            pass
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
# РАЗБОР ПУТИ С НОРМАЛИЗАЦИЕЙ
# ============================================================
def normalize_key(key):
    return ' '.join(str(key).strip().split())

def resolve_path(path_parts):
    node = kb['main_menu']
    actual_keys = []
    for i, part in enumerate(path_parts):
        norm = normalize_key(part).lower()
        found_key = None
        for key in node.keys():
            if normalize_key(key).lower() == norm:
                found_key = key
                break
        if found_key is None:
            return None, None, None
        actual_keys.append(found_key)
        val = node[found_key]
        if i == len(path_parts) - 1:
            return val, actual_keys, val.get('type')
        if val.get('type') == 'category':
            node = val['children']
        else:
            return None, None, 'content'
    return None, None, None

def get_children(path_parts):
    if not path_parts:
        return kb['main_menu']
    val, _, ntype = resolve_path(path_parts)
    if ntype == 'category':
        return val['children']
    return None

def parse_quoted(text):
    text = text.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end == -1:
            return None, None
        path_str = text[1:end].strip('/')
        rest = text[end+1:].strip()
        if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
            second = rest[1:-1]
        else:
            second = rest.strip('"').strip()
        return path_str, second
    else:
        last_space = text.rfind(' ')
        if last_space == -1:
            return None, None
        path_str = text[:last_space].strip('/ ')
        second = text[last_space+1:].strip()
        return path_str, second

def split_path(path_str):
    return [p.strip() for p in path_str.split('/') if p.strip()]

# ============================================================
# ПОИСК
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
        mode         = album.get("mode", "add")
        del album_cache[media_group_id]
        album_timers.pop(media_group_id, None)

    data = files[0] if len(files) == 1 else files
    with user_states_lock:
        user_states[user_id] = {
            "step": "waiting_label", "path": path_parts,
            "content_type": content_type, "data": data,
            "caption": caption, "is_album": len(files) > 1,
            "mode": mode
        }

    if mode == 'set':
        _save_content_direct(user_id, chat_id, label=None)
    else:
        bot.send_message(chat_id, f"📸 {'Альбом' if len(files) > 1 else 'Фото'} собран ({len(files)} шт.).\nВведите название кнопки:", parse_mode="HTML")

def _save_content_direct(user_id, chat_id, label):
    with user_states_lock:
        state = user_states.get(user_id)
        if not state:
            return
        path_parts   = state.get("path", [])
        content_type = state.get("content_type")
        data         = state.get("data")
        caption      = state.get("caption", "")
        mode         = state.get("mode", "add")

    if mode == 'set':
        parent = get_children(path_parts[:-1])
        if parent is None:
            bot.send_message(chat_id, f"❌ Путь не найден: {escape_html('/'.join(path_parts[:-1]))}")
            with user_states_lock:
                user_states.pop(user_id, None)
            return
        norm = normalize_key(path_parts[-1]).lower()
        real_key = None
        for k in parent.keys():
            if normalize_key(k).lower() == norm:
                real_key = k
                break
        if real_key is None:
            bot.send_message(chat_id, f"❌ Материал не найден.")
            with user_states_lock:
                user_states.pop(user_id, None)
            return
        entry = {"type": "content", "content_type": content_type, "data": data}
        if caption:
            entry["caption"] = caption
        parent[real_key] = entry
        save_json('knowledge_base.json', kb)
        _invalidate_search_cache()
        with user_states_lock:
            user_states.pop(user_id, None)
        bot.send_message(chat_id, f"✅ Материал '{real_key}' обновлён.")
        notify_admins(f"📝 Админ {user_id} обновил '{real_key}'")
        return

    parent = get_children(path_parts)
    if parent is None:
        bot.send_message(chat_id, f"❌ Путь не найден: {escape_html('/'.join(path_parts))}")
        with user_states_lock:
            user_states.pop(user_id, None)
        return
    new_key = label
    norm = normalize_key(new_key).lower()
    if any(normalize_key(k).lower() == norm for k in parent.keys()):
        suffix = 1
        while any(normalize_key(f"{new_key}_{suffix}").lower() == normalize_key(k).lower() for k in parent.keys()):
            suffix += 1
        new_key = f"{new_key}_{suffix}"
        bot.send_message(chat_id, f"⚠️ Название занято. Использую '{new_key}'.")
    entry = {"type": "content", "content_type": content_type, "data": data}
    if caption:
        entry["caption"] = caption
    parent[new_key] = entry
    save_json('knowledge_base.json', kb)
    _invalidate_search_cache()
    with user_states_lock:
        user_states.pop(user_id, None)
    bot.send_message(chat_id, f"✅ Материал добавлен как '{new_key}'.")
    notify_admins(f"📄 Админ {user_id} добавил '{new_key}' в /{'/'.join(path_parts)}/")

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
    except Exception as e:
        logger.error(f"Ошибка /start: {e}")

@bot.message_handler(commands=['id', 'myid'])
def cmd_id(message):
    bot.send_message(message.chat.id, f"🆔 Ваш Телеграм ID: <code>{message.from_user.id}</code>\n\nПередайте его администратору для получения доступа.", parse_mode="HTML")

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
        '/addcategory "путь" "название" — создать папку\n'
        '/addcontent "путь" тип — добавить материал в папку\n'
        '/setcontent "путь" тип — ЗАМЕНИТЬ существующий материал\n'
        '/delete "путь" — удалить элемент\n'
        "/reloadkb — перезагрузить базу знаний\n"
        "/restart — сбросить/переустановить вебхук (админ)\n"
        "/webhookinfo — статус вебхука (админ)\n\n"
        "<b>Примеры:</b>\n"
        '<code>/addcontent "Отдел продаж/Материалы" text</code>\n'
        '<code>/setcontent "Отдел продаж/Материалы/Оферта" text_with_link</code>\n'
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
        f"🔍 Запросов в кеше: {len(_search_cache)}"
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
        bot.send_message(message.chat.id, f"⚠️ {new_admin} уже администратор.")
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
    if len(tree) > 3800:
        chunks = [tree[i:i+3800] for i in range(0, len(tree), 3800)]
        for i, chunk in enumerate(chunks):
            header = "📋 <b>Структура (продолжение):</b>\n" if i > 0 else "📋 <b>Структура базы знаний:</b>\n"
            bot.send_message(message.chat.id, header + f"<pre>{escape_html(chunk)}</pre>", parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, f"📋 <b>Структура базы знаний:</b>\n<pre>{escape_html(tree)}</pre>", parse_mode="HTML")

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
        bot.send_message(message.chat.id, "✅ База знаний перезагружена. Кеш сброшен.")
        notify_admins(f"🔄 Админ {message.from_user.id} перезагрузил базу.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка перезагрузки: {e}")

@bot.message_handler(commands=['addcategory'])
def cmd_add_category(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id,
            'ℹ️ Использование:\n<code>/addcategory "путь" "Название"</code>\n\n'
            'Примеры:\n'
            '<code>/addcategory "Отдел продаж/Возражения" "Новый тип"</code>\n'
            '<code>/addcategory "" "Новый отдел"</code> — в корне',
            parse_mode="HTML")
        return
    path_str, new_name = parse_quoted(parts[1])
    if path_str is None or not new_name:
        bot.send_message(message.chat.id,
            '❌ Неверный формат. Используй кавычки:\n<code>/addcategory "Отдел продаж" "Новый раздел"</code>',
            parse_mode="HTML")
        return
    path_parts = split_path(path_str)
    parent = get_children(path_parts)
    if parent is None:
        val, _, ntype = resolve_path(path_parts)
        if ntype == 'content':
            bot.send_message(message.chat.id,
                f"❌ <b>{escape_html('/'.join(path_parts))}</b> — это материал, а не папка.\n"
                f"Категорию можно создать только внутри папки.",
                parse_mode="HTML")
            return
        bot.send_message(message.chat.id, f"❌ Путь не найден: {escape_html('/'.join(path_parts))}")
        return
    norm_name = normalize_key(new_name).lower()
    for existing in parent.keys():
        if normalize_key(existing).lower() == norm_name:
            bot.send_message(message.chat.id, f"⚠️ Элемент '{new_name}' уже существует.")
            return
    parent[new_name] = {"type": "category", "children": {}}
    save_json('knowledge_base.json', kb)
    _invalidate_search_cache()
    location = f"/{'/'.join(path_parts)}/" if path_parts else "корень меню"
    bot.send_message(message.chat.id, f"✅ Папка <b>{escape_html(new_name)}</b> создана в {escape_html(location)}.", parse_mode="HTML")

@bot.message_handler(commands=['delete'])
def cmd_delete(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id,
            'ℹ️ Использование: /delete "путь"\nПример: /delete "Отдел продаж/Бонусы"',
            parse_mode="HTML")
        return
    raw = parts[1].strip().strip('"')
    path_parts = split_path(raw)
    if not path_parts:
        bot.send_message(message.chat.id, "❌ Нельзя удалить главное меню.")
        return
    parent = get_children(path_parts[:-1])
    if parent is None:
        bot.send_message(message.chat.id, f"❌ Путь не найден: {escape_html('/'.join(path_parts[:-1]))}")
        return
    norm_target = normalize_key(path_parts[-1]).lower()
    found = None
    for key in parent.keys():
        if normalize_key(key).lower() == norm_target:
            found = key
            break
    if found:
        del parent[found]
        save_json('knowledge_base.json', kb)
        _invalidate_search_cache()
        bot.send_message(message.chat.id, f"✅ Элемент <b>{escape_html(found)}</b> удалён.", parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, f"❌ Элемент '{path_parts[-1]}' не найден.")

def _start_content_input(message, path_parts, content_type, mode):
    with user_states_lock:
        user_states[message.from_user.id] = {
            "step": "waiting_data",
            "path": path_parts,
            "content_type": content_type,
            "mode": mode
        }
    type_hints = {
        "text":          "напишите текст",
        "text_with_link":"напишите текст с HTML-ссылками: &lt;a href='url'&gt;текст&lt;/a&gt;",
        "photo":         "отправьте фото (можно альбом)",
        "document":      "отправьте файл (PDF и др.)"
    }
    action = "ЗАМЕНА материала" if mode == 'set' else "Новый материал в папке"
    bot.send_message(message.chat.id,
        f"✏️ {action}: <b>/{'/'.join(path_parts)}/</b>\n"
        f"Тип: <b>{content_type}</b>\n\n"
        f"Действие: {type_hints.get(content_type, 'отправьте контент')}\n\n"
        f"Для отмены: /cancel",
        parse_mode="HTML")

@bot.message_handler(commands=['addcontent'])
def cmd_add_content_start(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id,
            'ℹ️ Использование:\n<code>/addcontent "путь к ПАПКЕ" тип</code>\n'
            "Типы: text, photo, document, text_with_link\n\n"
            'Пример: <code>/addcontent "Отдел продаж/Материалы" text</code>',
            parse_mode="HTML")
        return
    path_str, content_type = parse_quoted(parts[1])
    if path_str is None or not content_type:
        bot.send_message(message.chat.id,
            '❌ Неверный формат:\n<code>/addcontent "Отдел продаж/Материалы" text</code>',
            parse_mode="HTML")
        return
    if content_type not in ('text', 'photo', 'document', 'text_with_link'):
        bot.send_message(message.chat.id, f"❌ Неизвестный тип: {content_type}")
        return
    path_parts = split_path(path_str)
    parent = get_children(path_parts)
    if parent is None:
        val, _, ntype = resolve_path(path_parts)
        if ntype == 'content':
            bot.send_message(message.chat.id,
                f"❌ <b>{escape_html('/'.join(path_parts))}</b> — это уже готовый материал, а не папка.\n\n"
                f"• Чтобы ЗАМЕНИТЬ его содержимое:\n"
                f'<code>/setcontent "{escape_html("/".join(path_parts))}" {content_type}</code>',
                parse_mode="HTML")
            return
        bot.send_message(message.chat.id, f"❌ Путь не найден: {escape_html('/'.join(path_parts))}")
        return
    _start_content_input(message, path_parts, content_type, mode='add')

@bot.message_handler(commands=['setcontent'])
def cmd_set_content_start(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id,
            'ℹ️ Использование:\n<code>/setcontent "путь к МАТЕРИАЛУ" тип</code>\n\n'
            'Пример: <code>/setcontent "Отдел продаж/Материалы/Оферта" text_with_link</code>',
            parse_mode="HTML")
        return
    path_str, content_type = parse_quoted(parts[1])
    if path_str is None or not content_type:
        bot.send_message(message.chat.id, '❌ Неверный формат. Используй кавычки.', parse_mode="HTML")
        return
    if content_type not in ('text', 'photo', 'document', 'text_with_link'):
        bot.send_message(message.chat.id, f"❌ Неизвестный тип: {content_type}")
        return
    path_parts = split_path(path_str)
    val, actual_keys, ntype = resolve_path(path_parts)
    if ntype is None:
        bot.send_message(message.chat.id, f"❌ Путь не найден: {escape_html('/'.join(path_parts))}")
        return
    if ntype == 'category':
        bot.send_message(message.chat.id,
            f"❌ <b>{escape_html('/'.join(path_parts))}</b> — это папка, а не материал.\n"
            f"Для добавления материала внутрь папки используй /addcontent",
            parse_mode="HTML")
        return
    _start_content_input(message, actual_keys, content_type, mode='set')

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
    results = search_kb(query)
    if not results:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
        bot.send_message(message.chat.id, f"🔍 По запросу <b>{query_safe}</b> ничего не найдено.", parse_mode="HTML", reply_markup=markup)
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
# ФУНКЦИЯ ДЛЯ ПОЛУЧЕНИЯ URL ВЕБХУКА
# ============================================================
def get_webhook_url():
    """Возвращает URL вебхука из переменных окружения или None."""
    webhook_url = os.getenv('WEBHOOK_URL')
    if not webhook_url:
        railway_domain = os.getenv('RAILWAY_PUBLIC_DOMAIN')
        if railway_domain:
            webhook_url = f"https://{railway_domain}/webhook"
            if not webhook_url.startswith('https://'):
                webhook_url = 'https://' + webhook_url.lstrip('http://')
    # Надёжная замена протокола, если вдруг остался http://
    if webhook_url and webhook_url.startswith('http://'):
        webhook_url = 'https://' + webhook_url[len('http://'):]
    return webhook_url

# ============================================================
# ОБНОВЛЁННАЯ КОМАНДА /restart
# ============================================================
@bot.message_handler(commands=['restart'])
def cmd_restart(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return

    webhook_url = get_webhook_url()
    try:
        delete_result = bot.delete_webhook(drop_pending_updates=True)
        if delete_result:
            logger.info("Webhook удалён через /restart")
        else:
            logger.warning("Не удалось удалить webhook через /restart (возможно, его не было)")

        if webhook_url:
            time.sleep(1)
            result = bot.set_webhook(url=webhook_url)
            if result:
                msg = "✅ Вебхук переустановлен."
                logger.info("Вебхук переустановлен через /restart")
            else:
                msg = "❌ Не удалось переустановить вебхук. Проверьте логи."
                logger.error("Ошибка переустановки вебхука через /restart")
        else:
            msg = "✅ Webhook сброшен. Бот в режиме polling."
        bot.send_message(message.chat.id, msg)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
        logger.error(f"Ошибка /restart: {e}")

# ============================================================
# ОБНОВЛЁННАЯ КОМАНДА /webhookinfo (точный режим)
# ============================================================
@bot.message_handler(commands=['webhookinfo'])
def cmd_webhook_info(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    try:
        info = bot.get_webhook_info()
        mode = "Вебхук" if info.url else "Polling"
        text = (
            f"🌐 <b>Информация о вебхуке</b>\n"
            f"Режим: <b>{mode}</b>\n"
            f"URL: {info.url or 'не задан'}\n"
            f"Ожидающие обновления: {info.pending_update_count}\n"
            f"Последняя ошибка: {info.last_error_message or 'нет'}\n"
            f"Макс. соединений: {info.max_connections}"
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка получения информации: {e}")

# ============================================================
# ЕДИНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ============================================================
@bot.message_handler(content_types=['text', 'photo', 'document'])
def handle_all_messages(message):
    user_id = message.from_user.id
    with user_states_lock:
        state = user_states.get(user_id)

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
                            "caption": message.caption or "",
                            "mode": state.get("mode", "add")
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
                _maybe_finish_set(message, user_id)
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
        _maybe_finish_set(message, user_id)
        return

    if state and state.get("step") == "waiting_label":
        if not message.text:
            bot.send_message(message.chat.id, "❌ Название должно быть текстом.")
            return
        _save_content_direct(user_id, message.chat.id, label=message.text.strip())
        return

    # Обычное сообщение
    if user_id in get_allowed_set():
        if message.photo or message.document:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
            bot.send_message(message.chat.id, "📌 Используйте /start или кнопки меню.", reply_markup=markup)
            return
        if message.text and len(message.text.strip()) > 2:
            query = message.text.strip()
            if len(query) > 50:
                query = query[:50]
            query_safe = escape_html(query)
            results = search_kb(query)
            markup = types.InlineKeyboardMarkup()
            if results:
                for r in results[:5]:
                    cb = "go|" + r['title']
                    if len(cb.encode('utf-8')) <= 64:
                        markup.add(types.InlineKeyboardButton(f"📄 {r['title']}", callback_data=cb))
                markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
                bot.send_message(message.chat.id,
                    f"🔍 Нашёл по запросу <b>{query_safe}</b>:\n\n" +
                    "\n".join(f"• {escape_html(r['title'])}" for r in results[:5]),
                    parse_mode="HTML", reply_markup=markup)
            else:
                markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
                bot.send_message(message.chat.id,
                    f"🔍 По запросу <b>{query_safe}</b> ничего не найдено.",
                    parse_mode="HTML", reply_markup=markup)
        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
            bot.send_message(message.chat.id, "📌 Напишите слово для поиска или /start", reply_markup=markup)
    else:
        bot.send_message(message.chat.id, "🚫 Доступ закрыт. Напишите /id и передайте ваш ID администратору.")

def _maybe_finish_set(message, user_id):
    with user_states_lock:
        state = user_states.get(user_id)
        if not state:
            return
        mode = state.get("mode", "add")
    if mode == 'set':
        _save_content_direct(user_id, message.chat.id, label=None)
    else:
        bot.send_message(message.chat.id, "🔑 Введите название кнопки.\nПример: <b>Новый материал</b>", parse_mode="HTML")

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
            full_path = path_parts + [name]
            val, actual_keys, ntype = resolve_path(full_path)
            if ntype is None:
                results = search_kb(name)
                match = next((r for r in results if normalize_key(r['title']).lower() == normalize_key(name).lower()), None)
                if match:
                    val2, ak2, nt2 = resolve_path(match['path'])
                    if nt2 == 'content':
                        send_content_node(chat_id, message_id, val2, match['path'])
                        user_context[chat_id] = match['path']
                        update_stats(user_id)
                        answer_query(call.id)
                        return
                answer_query(call.id, "Раздел не найден")
                return
            if ntype == 'content':
                send_content_node(chat_id, message_id, val, actual_keys)
                user_context[chat_id] = actual_keys
            else:
                show_category_menu(chat_id, message_id, val['children'], actual_keys)
                user_context[chat_id] = actual_keys
            update_stats(user_id)
            answer_query(call.id)
            return
        answer_query(call.id, "Неизвестная команда")
    except Exception as e:
        logger.error(f"Ошибка при кнопке '{data}': {e}")
        answer_query(call.id, "Произошла ошибка, попробуйте ещё раз", alert=True)

# ============================================================
# ЗАПУСК С ВЕБХУКОМ
# ============================================================
if __name__ == "__main__":
    logger.info("🚀 Бот ДЦША запускается...")

    webhook_url = get_webhook_url()

    if webhook_url:
        logger.info(f"Режим вебхука: {webhook_url}")

        bot.delete_webhook(drop_pending_updates=True)
        time.sleep(1)
        result = bot.set_webhook(url=webhook_url)
        if result:
            logger.info("Вебхук установлен успешно.")
            notify_admins("✅ Бот запущен и работает через вебхук.")
        else:
            logger.critical("❌ Не удалось установить вебхук! Бот не будет получать обновления. Завершаю работу.")
            notify_admins("🚨 КРИТИЧЕСКАЯ ОШИБКА: не удалось установить вебхук! Бот остановлен.", is_error=True)
            exit(1)

        app = Flask(__name__)

        @app.route('/webhook', methods=['POST'])
        def webhook():
            if request.headers.get('content-type') == 'application/json':
                json_string = request.get_data().decode('utf-8')
                update = types.Update.de_json(json_string)
                try:
                    bot.process_new_updates([update])
                except Exception as e:
                    logger.error(f"Ошибка обработки обновления: {e}")
                return '', 200
            else:
                return 'Unsupported Media Type', 415

        @app.route('/')
        def index():
            return 'Bot is running', 200

        port = int(os.environ.get('PORT', 5000))
        app.run(host='0.0.0.0', port=port, threaded=False)

    else:
        logger.info("Режим polling (локальная разработка или нет домена)")
        try:
            bot.delete_webhook(drop_pending_updates=True)
        except:
            pass
        time.sleep(2)

        notify_admins("✅ Бот запущен (polling).")

        while True:
            try:
                bot.polling(
                    timeout=60,
                    long_polling_timeout=30,
                    allowed_updates=["message", "callback_query"],
                    interval=0,
                    non_stop=True
                )
            except ApiTelegramException as e:
                if e.error_code == 409:
                    logger.warning("409 Conflict в polling – жду 5 сек.")
                    time.sleep(5)
                    try:
                        bot.delete_webhook(drop_pending_updates=True)
                    except:
                        pass
                    continue
                else:
                    logger.critical(f"Telegram API ошибка {e.error_code}: {e}. Жду 5 сек.")
                    time.sleep(5)
                    continue
            except Exception as e:
                logger.critical(f"❌ Бот упал: {e}. Перезапуск через 5 сек.")
                time.sleep(5)
                continue
