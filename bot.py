import telebot
import os
import logging
from dotenv import load_dotenv
load_dotenv()
import time
import json
from telebot import types
from data import (
    MENU, SCRIPT, SCRIPT_ITEMS, OBJECTIONS, OBJ_ITEMS,
    PRICES_TEXT, FINANCE_TEXT, FOLLOWUP_TEXT, DIAGNOSTIC_TEXT,
    MEDIA_ITEMS
)

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
# ФУНКЦИИ РАБОТЫ С ФАЙЛАМИ
# ============================================================

DATA_DIR = os.path.dirname(os.path.abspath(__file__))

def load_json(filename, default):
    """Загружает JSON, при ошибке создаёт новый файл с default."""
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, IOError) as e:
        logger.warning(f"Ошибка чтения {filename}: {e}. Создаю новый файл.")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default

def save_json(filename, data):
    """Безопасно сохраняет JSON."""
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
ALLOWED_USERS = load_json('allowed_users.json', DEFAULT_ALLOWED)

DEFAULT_ADMINS = [882100075]
ADMIN_IDS = load_json('admin_ids.json', DEFAULT_ADMINS)

# Статистика: теперь храним только счётчики, без списка ID
DEFAULT_STATS = {"user_count": 0, "clicks": 0, "last_activity": None}
stats = load_json('stats.json', DEFAULT_STATS)

# Множество для отслеживания уже учтённых пользователей в текущей сессии
# (не сохраняется в файл, только для уведомлений)
_seen_users = set()

def get_allowed_set():
    return set(ALLOWED_USERS)

# ============================================================
# ТОКЕН
# ============================================================
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    # Для локальной разработки раскомментируй:
    #TOKEN = 'ОШИБКА'
    logger.error("=" * 50)
    logger.error("❌ ТОКЕН НЕ НАЙДЕН!")
    logger.error("Если локально — впиши токен в коде.")
    logger.error("Если на Railway — добавь переменную BOT_TOKEN.")
    logger.error("=" * 50)
    exit(1)

bot = telebot.TeleBot(TOKEN)

# ============================================================
# ПРОВЕРКА data.py
# ============================================================
def check_data_integrity():
    required = [MENU, SCRIPT, SCRIPT_ITEMS, OBJECTIONS, OBJ_ITEMS,
                PRICES_TEXT, FINANCE_TEXT, FOLLOWUP_TEXT, DIAGNOSTIC_TEXT,
                MEDIA_ITEMS]
    for item in required:
        if item is None:
            logger.critical("❌ Ошибка в data.py: один из разделов отсутствует!")
            exit(1)
    logger.info("✅ data.py загружен корректно.")

check_data_integrity()

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def check_access(user_id, chat_id):
    if user_id not in get_allowed_set():
        try:
            bot.send_message(chat_id, "🚫 Доступ закрыт. Обратитесь к администратору.")
        except Exception:
            pass
        logger.info(f"⛔ Отказано в доступе: ID {user_id}")
        return False
    return True

def is_admin(user_id):
    return user_id in ADMIN_IDS

def make_keyboard(buttons):
    markup = types.InlineKeyboardMarkup()
    for btn in buttons:
        markup.add(types.InlineKeyboardButton(text=btn[0], callback_data=btn[1]))
    return markup

def back_to_main():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
    return markup

def back_to_section(section_name, section_label):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(f"← {section_label}", callback_data=section_name))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="main"))
    return markup

def safe_delete_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение {message_id}: {e}")

def safe_edit(text, chat_id, message_id, keyboard):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard)
    except Exception as e:
        logger.warning(f"Не удалось отредактировать: {e}")
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        try:
            bot.send_message(chat_id, text, reply_markup=keyboard)
        except Exception as e2:
            logger.error(f"Не удалось отправить новое сообщение: {e2}")

def send_media(chat_id, item):
    mtype = item.get("type")
    caption = item.get("caption", "")
    keyboard = back_to_main()
    try:
        if mtype == "photo":
            bot.send_photo(chat_id, photo=item["url"], caption=caption,
                           reply_markup=keyboard, parse_mode="HTML")
        elif mtype == "document":
            bot.send_document(chat_id, document=item["url"], caption=caption,
                              reply_markup=keyboard, parse_mode="HTML")
        elif mtype == "text_with_link":
            bot.send_message(chat_id, text=item["text"], parse_mode="HTML",
                             reply_markup=keyboard, disable_web_page_preview=False)
        elif mtype == "photo_with_link":
            bot.send_photo(chat_id, photo=item["url"], caption=caption, parse_mode="HTML")
            bot.send_message(chat_id, text=item.get("link_text", ""),
                             parse_mode="HTML", reply_markup=keyboard)
        else:
            bot.send_message(chat_id, "⚠️ Этот материал ещё не настроен.", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Ошибка отправки медиа: {e}")
        bot.send_message(chat_id, "❌ Не удалось загрузить материал. Проверьте ссылку или file_id.",
                         reply_markup=keyboard)

def notify_admins(text, is_error=False):
    """Отправляет сообщение всем админам. Для ошибок добавляет 🚨."""
    if is_error:
        text = f"🚨 {text}"
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text)
        except Exception:
            pass

def update_stats(user_id):
    """Обновляет статистику: увеличивает счётчики и записывает время."""
    global _seen_users
    # Проверяем, был ли пользователь уже учтён в текущей сессии
    if user_id not in _seen_users:
        _seen_users.add(user_id)
        stats["user_count"] += 1
    stats["clicks"] += 1
    stats["last_activity"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json('stats.json', stats)

# ============================================================
# КОМАНДЫ
# ============================================================

@bot.message_handler(commands=['start', 'menu'])
def start(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    # Уведомление админов, если пользователь в белом списке и впервые запускает бота
    if user_id in get_allowed_set() and user_id not in _seen_users:
        name = message.from_user.first_name or "Без имени"
        notify_admins(f"👤 Новый пользователь: {name} (ID: {user_id}) запустил бота.")

    if not check_access(user_id, chat_id):
        return

    try:
        bot.send_message(
            chat_id,
            MENU["main"]["text"],
            reply_markup=make_keyboard(MENU["main"]["buttons"])
        )
        update_stats(user_id)
        logger.info(f"✅ Пользователь {user_id} запустил бота")
    except Exception as e:
        logger.error(f"Ошибка в /start: {e}")
        notify_admins(f"Ошибка в /start: {e}", is_error=True)

@bot.message_handler(commands=['id'])
def get_id(message):
    bot.send_message(
        message.chat.id,
        f"Ваш Телеграм ID: {message.from_user.id}\n\n"
        f"Передайте его администратору чтобы получить доступ."
    )

@bot.message_handler(commands=['help'])
def help_command(message):
    help_text = (
        "🤖 <b>Бот-помощник менеджера ДЦША</b>\n\n"
        "Доступные команды:\n"
        "/start или /menu — главное меню\n"
        "/id — показать ваш ID\n"
        "/help — эта справка\n\n"
        "<b>Для администраторов:</b>\n"
        "/stats — статистика использования\n"
        "/listusers — список разрешённых пользователей\n"
        "/listadmins — список администраторов\n"
        "/adduser &lt;ID&gt; — добавить пользователя\n"
        "/removeuser &lt;ID&gt; — удалить пользователя\n"
        "/addadmin &lt;ID&gt; — добавить администратора\n\n"
        "Используйте кнопки для навигации по информации."
    )
    bot.send_message(message.chat.id, help_text, parse_mode="HTML")

@bot.message_handler(commands=['listusers'])
def list_users(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    if not ALLOWED_USERS:
        bot.send_message(message.chat.id, "📭 Список пользователей пуст.")
        return
    text = "📋 <b>Список разрешённых пользователей:</b>\n" + "\n".join(f"• <code>{uid}</code>" for uid in ALLOWED_USERS)
    bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(commands=['listadmins'])
def list_admins(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    if not ADMIN_IDS:
        bot.send_message(message.chat.id, "📭 Список администраторов пуст.")
        return
    text = "👑 <b>Список администраторов:</b>\n" + "\n".join(f"• <code>{uid}</code>" for uid in ADMIN_IDS)
    bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(commands=['adduser'])
def add_user(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ Использование: /adduser <ID>\nПример: /adduser 123456789")
        return
    try:
        new_id = int(args[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    if new_id in get_allowed_set():
        bot.send_message(message.chat.id, f"⚠️ Пользователь {new_id} уже есть в списке.")
        return
    ALLOWED_USERS.append(new_id)
    save_json('allowed_users.json', ALLOWED_USERS)
    bot.send_message(message.chat.id, f"✅ Пользователь {new_id} добавлен в белый список.")
    notify_admins(f"➕ Админ {message.from_user.id} добавил пользователя {new_id}.")

@bot.message_handler(commands=['removeuser'])
def remove_user(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ Использование: /removeuser <ID>\nПример: /removeuser 123456789")
        return
    try:
        rem_id = int(args[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    if rem_id not in get_allowed_set():
        bot.send_message(message.chat.id, f"⚠️ Пользователь {rem_id} не найден в списке.")
        return
    ALLOWED_USERS.remove(rem_id)
    save_json('allowed_users.json', ALLOWED_USERS)
    bot.send_message(message.chat.id, f"✅ Пользователь {rem_id} удалён из белого списка.")
    notify_admins(f"➖ Админ {message.from_user.id} удалил пользователя {rem_id}.")

@bot.message_handler(commands=['addadmin'])
def add_admin(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "ℹ️ Использование: /addadmin <ID>\nПример: /addadmin 123456789")
        return
    try:
        new_admin = int(args[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    if new_admin in ADMIN_IDS:
        bot.send_message(message.chat.id, f"⚠️ Пользователь {new_admin} уже администратор.")
        return
    ADMIN_IDS.append(new_admin)
    save_json('admin_ids.json', ADMIN_IDS)
    bot.send_message(message.chat.id, f"✅ Пользователь {new_admin} теперь администратор.")
    notify_admins(f"👑 Админ {message.from_user.id} назначил администратора {new_admin}.")

@bot.message_handler(commands=['stats'])
def stats_command(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 Только для администраторов.")
        return
    text = (
        f"📊 <b>Статистика бота</b> (с последнего запуска)\n"
        f"👥 Уникальных пользователей: {stats['user_count']}\n"
        f"🖱 Нажатий на кнопки: {stats['clicks']}\n"
        f"🕒 Последняя активность: {stats['last_activity'] or '—'}\n"
        f"📋 Всего в белом списке: {len(ALLOWED_USERS)}"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")

# Обработка обычных текстовых сообщений
@bot.message_handler(func=lambda message: True)
def echo_all(message):
    if message.from_user.id in get_allowed_set():
        bot.send_message(
            message.chat.id,
            "📌 Используйте кнопки меню, чтобы получить информацию.",
            reply_markup=back_to_main()
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
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    message_id = call.message.message_id

    if not check_access(user_id, chat_id):
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    d = call.data
    handled = True

    logger.info(f"Кнопка '{d}' от {user_id}")

    try:
        if d == "main":
            safe_edit(MENU["main"]["text"], chat_id, message_id,
                      make_keyboard(MENU["main"]["buttons"]))

        elif d == "script":
            safe_edit(SCRIPT["text"], chat_id, message_id,
                      make_keyboard(SCRIPT["buttons"]))

        elif d in SCRIPT_ITEMS:
            safe_edit(SCRIPT_ITEMS[d], chat_id, message_id,
                      back_to_section("script", "Скрипт встречи"))

        elif d == "objections":
            safe_edit(OBJECTIONS["text"], chat_id, message_id,
                      make_keyboard(OBJECTIONS["buttons"]))

        elif d in OBJ_ITEMS:
            safe_edit(OBJ_ITEMS[d], chat_id, message_id,
                      back_to_section("objections", "Возражения"))

        elif d == "prices":
            safe_edit(PRICES_TEXT, chat_id, message_id, back_to_main())

        elif d == "finance":
            safe_edit(FINANCE_TEXT, chat_id, message_id, back_to_main())

        elif d == "followup":
            safe_edit(FOLLOWUP_TEXT, chat_id, message_id, back_to_main())

        elif d == "diagnostic":
            safe_edit(DIAGNOSTIC_TEXT, chat_id, message_id, back_to_main())

        elif d == "media":
            safe_edit("📎 Материалы и ссылки:", chat_id, message_id,
                      make_keyboard([
                          ["🖼️ Презентация центра", "media_presentation"],
                          ["📄 Оферта (PDF)", "media_offer"],
                          ["📊 Статистика центра", "media_stats"],
                          ["🔗 Полезные ссылки", "media_links"],
                          ["📸 Кейсы клиентов", "media_cases"],
                          ["🏠 Главное меню", "main"],
                      ]))

        elif d.startswith("media_") and d in MEDIA_ITEMS:
            safe_delete_message(chat_id, message_id)
            send_media(chat_id, MEDIA_ITEMS[d])

        else:
            handled = False

        if handled:
            update_stats(user_id)

    except Exception as e:
        logger.error(f"Ошибка при обработке кнопки '{d}': {e}")
        notify_admins(f"Ошибка при кнопке '{d}': {e}", is_error=True)
        try:
            bot.answer_callback_query(call.id, "❌ Произошла ошибка, попробуйте ещё раз", show_alert=True)
        except Exception:
            pass
        return

    try:
        if handled:
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "⚠️ Раздел в разработке", show_alert=False)
    except Exception:
        pass

# ============================================================
# ЗАПУСК С АВТОПЕРЕЗАПУСКОМ
# ============================================================

if __name__ == "__main__":
    logger.info("✅ Бот ДЦША запущен и готов к работе!")
    notify_admins("✅ Бот запущен и работает.")

    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            error_text = f"Бот упал с ошибкой: {e}. Перезапуск через 5 секунд..."
            logger.critical(error_text)
            notify_admins(error_text, is_error=True)
            time.sleep(5)
