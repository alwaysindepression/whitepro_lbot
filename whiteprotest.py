import telebot
from telebot import types
import json
import os
import time
import threading
from datetime import datetime, timedelta
import requests
import locale

# Устанавливаем русскую локаль для даты
try:
    locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_TIME, 'Russian_Russia.1251')
    except locale.Error:
        pass

# ⚠️ ВНИМАНИЕ: этот токен уже был показан в переписке — перевыпусти его через
# @BotFather (/revoke), иначе им может воспользоваться кто угодно.
BOT_TOKEN = ''
bot = telebot.TeleBot(BOT_TOKEN)

# Юзернейм бота (без @), нужен для сборки диплинков вида t.me/<bot>?start=...
# Определяется автоматически при запуске в блоке __main__.
BOT_USERNAME = None

# Файлы для хранения данных
USERS_FILE = 'users.json'
DEALS_FILE = 'deals.json'
ADMIN_FILE = 'admin.json'
TEMP_FILE = 'temp_data.json'

# Комиссия 5% (берётся при пополнении кошелька)
COMMISSION_RATE = 0.05

# Сколько минут даётся продавцу на принятие сделки / покупателю на оплату
ACCEPT_TIMEOUT_MIN = 15
PAYMENT_TIMEOUT_MIN = 15

# Сколько сделок показывать на одной странице истории сделок
DEALS_PER_PAGE = 3

# Номер, с которого начинается нумерация сделок (первая сделка получит именно этот номер)
START_DEAL_ID = 39089

# ============ БЛОКИРОВКА ФАЙЛОВ (защита от гонок при параллельных запросах) ============
_data_lock = threading.RLock()


def init_files():
    for file in [USERS_FILE, DEALS_FILE, ADMIN_FILE, TEMP_FILE]:
        if not os.path.exists(file):
            with open(file, 'w', encoding='utf-8') as f:
                json.dump({}, f)


def load_json(file):
    try:
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(file, data):
    tmp_path = file + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, file)  # атомарная замена файла


def load_users():
    with _data_lock:
        return load_json(USERS_FILE)


def save_users(data):
    with _data_lock:
        save_json(USERS_FILE, data)


def load_deals():
    with _data_lock:
        return load_json(DEALS_FILE)


def save_deals(data):
    with _data_lock:
        save_json(DEALS_FILE, data)


def load_admins():
    with _data_lock:
        return load_json(ADMIN_FILE).get('admins', [])


def save_admins(admins):
    with _data_lock:
        save_json(ADMIN_FILE, {"admins": admins})


def load_temp():
    with _data_lock:
        return load_json(TEMP_FILE)


def save_temp(data):
    with _data_lock:
        save_json(TEMP_FILE, data)


def get_balance(user_id):
    users = load_users()
    return users.get(str(user_id), {}).get('balance', 0.0)


def update_balance(user_id, amount):
    """Атомарно изменяет баланс: чтение и запись под одной блокировкой,
    чтобы параллельные операции не затирали друг друга."""
    with _data_lock:
        users = load_json(USERS_FILE)
        uid = str(user_id)
        if uid not in users:
            users[uid] = {'balance': 0, 'deals': [], 'reputation': 0,
                           'registered_at': datetime.now().isoformat()}
        users[uid]['balance'] = users[uid].get('balance', 0.0) + amount
        save_json(USERS_FILE, users)
        return users[uid]['balance']


def add_reputation(user_id, delta=1):
    with _data_lock:
        users = load_json(USERS_FILE)
        uid = str(user_id)
        if uid not in users:
            users[uid] = {'balance': 0, 'deals': [], 'reputation': 0,
                           'registered_at': datetime.now().isoformat()}
        users[uid]['reputation'] = users[uid].get('reputation', 0) + delta
        save_json(USERS_FILE, users)


def add_deal_to_user(user_id, deal_id):
    with _data_lock:
        users = load_json(USERS_FILE)
        uid = str(user_id)
        if uid not in users:
            users[uid] = {'balance': 0, 'deals': [], 'reputation': 0,
                           'registered_at': datetime.now().isoformat()}
        if deal_id not in users[uid].get('deals', []):
            users[uid].setdefault('deals', []).append(deal_id)
        save_json(USERS_FILE, users)


def get_user(user_id):
    with _data_lock:
        users = load_json(USERS_FILE)
        uid = str(user_id)
        if uid not in users:
            users[uid] = {
                'balance': 0,
                'deals': [],
                'reputation': 0,
                'registered_at': datetime.now().isoformat()
            }
            save_json(USERS_FILE, users)
        return users[uid]


def generate_deal_id():
    with _data_lock:
        deals = load_json(DEALS_FILE)
        if not deals:
            return START_DEAL_ID
        return max([int(d) for d in deals.keys()] + [START_DEAL_ID - 1]) + 1


# Форматирование даты на русском
def format_date_ru(date_string):
    try:
        dt = datetime.fromisoformat(date_string)
    except (ValueError, TypeError):
        dt = datetime.now()
    months = {
        1: 'января', 2: 'февраля', 3: 'марта', 4: 'апреля',
        5: 'мая', 6: 'июня', 7: 'июля', 8: 'августа',
        9: 'сентября', 10: 'октября', 11: 'ноября', 12: 'декабря'
    }
    return f"{dt.day} {months[dt.month]} {dt.year} года"


def format_amount(amount):
    """Убирает лишние нули: 50.0 -> '50', 45.5 -> '45.5'."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    if amount == int(amount):
        return str(int(amount))
    return f"{amount:.2f}".rstrip('0').rstrip('.')


def display_username(uid):
    """Возвращает username без @ (или сам ID строкой, если username нет).
    Работает по числовому ID чата, который Bot API отдаёт без ограничений,
    если бот уже взаимодействовал с этим пользователем."""
    try:
        chat = bot.get_chat(uid)
        return chat.username or str(uid)
    except telebot.apihelper.ApiException:
        return str(uid)


def resolve_user_id(identifier):
    """Пытается превратить @username / ссылку / ID в реальный telegram id.

    ВАЖНО: Bot API НЕ позволяет получить чат обычного приватного пользователя
    по его @username (это разрешено только для публичных каналов, групп и
    ботов) — поэтому раньше поиск по нику всегда падал с ошибкой, даже если
    человек уже писал боту. Правильный способ — искать по локальной базе
    пользователей, которую бот сам заполняет при каждом /start.
    """
    identifier = identifier.strip()
    if identifier.startswith('https://t.me/'):
        identifier = '@' + identifier.split('https://t.me/')[1].split('/')[0].split('?')[0]

    if identifier.startswith('@'):
        username = identifier[1:].lower()
        users = load_users()
        for uid, data in users.items():
            stored_username = (data.get('username') or '').lower()
            if stored_username and stored_username == username:
                return int(uid)
        # Локально не нашли — на всякий случай пробуем через Bot API
        # (сработает только для публичных каналов/групп/ботов).
        try:
            chat = bot.get_chat(identifier)
            return chat.id
        except telebot.apihelper.ApiException:
            return None

    try:
        return int(identifier)
    except ValueError:
        return None


# ============ ГЛАВНОЕ МЕНЮ ============
def main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Создать сделку (≈5%)", callback_data="create_deal"))
    markup.add(
        types.InlineKeyboardButton("Поиск", callback_data="search"),
        types.InlineKeyboardButton("Профиль", callback_data="profile")
    )
    markup.add(types.InlineKeyboardButton("💼 Кошелёк", callback_data="wallet"))
    markup.add(types.InlineKeyboardButton("Подключить", callback_data="connect"))
    return markup


import html as _html

# ID кастомных эмодзи Telegram, используемых в оформлении текстов бота.
CUSTOM_EMOJI = {
    'header': '6035084557378654059',  # перед @username (ID: ...)
    'reputation': '6050643982646513651',     # перед "Репутация: N шт."
    'like': '6041720006973067267',           # перед "0%" (лайки)
    'dislike': '6041716699848249286',        # перед "0%" (дизлайки)
    'deals': '5902206159095339799',          # перед "Сделок завершено: N шт."
    'total_amount': '5409048419211682843',   # перед "Общая сумма сделок: ..." / рядом с балансом
    'deposit': '5769126056262898415',        # перед "Депозит: ..."
    'status': '5886685105065300941',         # перед "Статус: ..."
    'registered': '5890937706803894250',     # перед "Зарегистрирован ..."
    'welcome': '6030445631921721471',        # перед приветственным текстом
    'search_prompt': '5429571366384842791',  # перед подсказкой поиска
    'request_sent': '5963103826075456248',   # перед "Запрос на сделку отправлен."
    'invite_header': '6033125983572201397',  # перед "@username, вас пригласили в сделку..."
    'conditions': '5778299625370817409',     # перед "Условия:"
}


def tg_emoji(key, fallback):
    """HTML-тег кастомного эмодзи Telegram с текстовым фолбэком для клиентов,
    которые не умеют их рендерить."""
    emoji_id = CUSTOM_EMOJI.get(key)
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


WELCOME_TEXT = (
    f"<b>{tg_emoji('welcome', '🛡')} FRK | Безопасность — твоя гарантия безопасности.\n\n"
    f"Здесь можно смотреть и сохранять репутацию, а при сомнениях — провести сделку у нас.</b>\n"
)

SEARCH_PROMPT_TEXT = (
    f"<b>{tg_emoji('search_prompt', '🔎')} Отправьте ссылку или ID на пользователя чей профиль хотите найти.</b>"
)


# ============ КОМАНДЫ ============
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    get_user(user_id)
    # запоминаем username, чтобы поиск по @username в принципе работал
    with _data_lock:
        users = load_json(USERS_FILE)
        users.setdefault(str(user_id), {})['username'] = message.from_user.username
        save_json(USERS_FILE, users)

    # Диплинк вида /start j-<deal_id> — переход по кнопке "Присоединиться"
    # из приглашения в сделку.
    parts = message.text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ''
    if payload.startswith('j-'):
        show_deal_join_screen(message, payload[2:])
        return

    bot.send_message(user_id, WELCOME_TEXT, reply_markup=main_menu(), parse_mode='HTML')


# ============ АДМИН ПАНЕЛЬ ============
def admin_tools(message):
    user_id = message.chat.id
    if user_id not in load_admins():
        bot.send_message(user_id, "❌ У вас нет доступа к админ панели.")
        return

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"),
        types.InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
        types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        types.InlineKeyboardButton("⚖️ Споры", callback_data="admin_disputes"),
        types.InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add"),
        types.InlineKeyboardButton("➖ Удалить админа", callback_data="admin_remove"),
    )
    bot.send_message(user_id, "👑 Админ панель\n\nВыберите действие:", reply_markup=markup)


@bot.message_handler(commands=['admintools'])
def admin_tools_command(message):
    admin_tools(message)


# ============ ОБРАБОТКА ИНЛАЙН КНОПОК ============
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id
    data = call.data

    # ===== ГЛАВНОЕ МЕНЮ =====
    if data == "create_deal":
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("Покупаю", callback_data="deal_buy"),
            types.InlineKeyboardButton("Продаю", callback_data="deal_sell"),
            types.InlineKeyboardButton("Назад", callback_data="back_main")
        )
        bot.edit_message_text("Что вы делаете?", call.message.chat.id, call.message.message_id,
                               reply_markup=markup)

    elif data == "search":
        bot.edit_message_text(
            SEARCH_PROMPT_TEXT,
            call.message.chat.id, call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Назад", callback_data="back_main")),
            parse_mode='HTML'
        )
        bot.register_next_step_handler(call.message, search_user)

    elif data == "profile":
        show_profile(user_id, user_id)

    elif data == "wallet":
        show_wallet(user_id)

    elif data == "wallet_deposit":
        show_deposit_menu(call)

    elif data == "wallet_withdraw":
        show_withdraw_menu(call)

    elif data == "noop":
        # Некликабельная строка-инфо (баланс / номер страницы) — просто гасим "часики" у кнопки
        bot.answer_callback_query(call.id)

    elif data == "connect":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            # Заменяем callback_data на url
            types.InlineKeyboardButton(
                "➕ Добавить в группу", url="https://t.me/whitepro_lbot?startgroup=true"),
            types.InlineKeyboardButton("Назад", callback_data="back_main")
        )
        bot.edit_message_text(
            "📋 Добавление бота в группу:\n\n"
            "1. Нажмите кнопку «Добавить в группу» ниже\n"
            "2. Выберите нужную группу\n"
            "3. Назначьте бота администратором\n"
            "4. После добавления напишите /start в группе",
            call.message.chat.id, call.message.message_id, reply_markup=markup
        )

    elif data == "my_deals":
        show_my_deals(call.message.chat.id, page=1)

    elif data.startswith("mydeals_page_"):
        page = int(data.split("_")[-1])
        show_my_deals(call.message.chat.id, message_id=call.message.message_id, page=page)

    elif data == "my_reputation":
        user_data = get_user(user_id)
        bot.answer_callback_query(call.id, f"⭐ Ваша репутация: {user_data.get('reputation', 0)}", show_alert=True)

    elif data.startswith("rep_menu_"):
        target_id = int(data.split("_")[-1])
        show_reputation_menu(call.message.chat.id, call.message.message_id, target_id)

    elif data.startswith("rep_back_"):
        target_id = int(data.split("_")[-1])
        profile_text, markup = build_profile(target_id)
        bot.edit_message_text(profile_text, call.message.chat.id, call.message.message_id,
                               reply_markup=markup, parse_mode='HTML')

    elif data.startswith("rep_all_"):
        target_id = int(data.split("_")[-1])
        show_reputation_result(call.message.chat.id, call.message.message_id, target_id, 'all')

    elif data.startswith("rep_lastpos_"):
        target_id = int(data.split("_")[-1])
        show_reputation_result(call.message.chat.id, call.message.message_id, target_id, 'lastpos')

    elif data.startswith("rep_lastneg_"):
        target_id = int(data.split("_")[-1])
        show_reputation_result(call.message.chat.id, call.message.message_id, target_id, 'lastneg')

    elif data.startswith("rep_pos_"):
        target_id = int(data.split("_")[-1])
        show_reputation_result(call.message.chat.id, call.message.message_id, target_id, 'pos')

    elif data.startswith("rep_neg_"):
        target_id = int(data.split("_")[-1])
        show_reputation_result(call.message.chat.id, call.message.message_id, target_id, 'neg')

    # ===== СОЗДАНИЕ СДЕЛКИ =====
    elif data in ["deal_buy", "deal_sell"]:
        temp = load_temp()
        temp[str(user_id)] = {'type': 'Покупаю' if data == "deal_buy" else 'Продаю', 'step': 'waiting_counterparty'}
        save_temp(temp)

        role = "продавца" if data == "deal_buy" else "покупателя"
        bot.edit_message_text(
            f"Отправьте ссылку или ID на продавца. {role}.",
            call.message.chat.id, call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Назад", callback_data="back_main"))
        )
        bot.register_next_step_handler(call.message, deal_step_handler)

    # ===== ПРИНЯТИЕ / ОТКЛОНЕНИЕ СДЕЛКИ ВТОРОЙ СТОРОНОЙ =====
    elif data.startswith("accept_deal_"):
        handle_deal_accept(call, accept=True)
    elif data.startswith("decline_deal_"):
        handle_deal_accept(call, accept=False)

    # ===== ОПЛАТА =====
    elif data.startswith("pay_deal_"):
        pay_deal(call)

    # ===== ПОДТВЕРЖДЕНИЕ ПОЛУЧЕНИЯ / СПОР =====
    elif data.startswith("confirm_deal_"):
        confirm_deal(call)
    elif data.startswith("dispute_deal_"):
        open_dispute(call)

    # ===== АДМИН РЕШЕНИЕ ПО СПОРУ =====
    elif data.startswith("resolve_seller_"):
        resolve_dispute(call, favor='seller')
    elif data.startswith("resolve_buyer_"):
        resolve_dispute(call, favor='buyer')

    # ===== АДМИНКА =====
    elif data == "admin_broadcast":
        if user_id not in load_admins():
            bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
            return
        bot.edit_message_text(
            "📤 Отправьте сообщение для рассылки.\n\nПоддерживается формат:\n- Текст\n- Фото\n- Видео\n- Документы",
            call.message.chat.id, call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Назад", callback_data="admin_back"))
        )
        bot.register_next_step_handler(call.message, process_broadcast)

    elif data == "admin_users":
        if user_id not in load_admins():
            bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
            return
        users = load_users()
        bot.answer_callback_query(call.id, f"👥 Всего пользователей: {len(users)}", show_alert=True)

    elif data == "admin_stats":
        if user_id not in load_admins():
            bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
            return
        users = load_users()
        deals = load_deals()
        total_balance = sum(u.get('balance', 0) for u in users.values())
        bot.answer_callback_query(
            call.id,
            f"📊 Статистика:\n👥 Пользователей: {len(users)}\n💰 Общий баланс: {total_balance:.2f} USDT\n📝 Сделок: {len(deals)}",
            show_alert=True
        )

    elif data == "admin_disputes":
        show_disputes(call)

    elif data == "admin_add":
        if user_id not in load_admins():
            bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
            return
        bot.edit_message_text(
            "Введите ID пользователя для добавления в админы:",
            call.message.chat.id, call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Назад", callback_data="admin_back"))
        )
        bot.register_next_step_handler(call.message, add_admin_process)

    elif data == "admin_remove":
        if user_id not in load_admins():
            bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
            return
        bot.edit_message_text(
            "Введите ID пользователя для удаления из админов:",
            call.message.chat.id, call.message.message_id,
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Назад", callback_data="admin_back"))
        )
        bot.register_next_step_handler(call.message, remove_admin_process)

    elif data == "admin_back":
        admin_tools(call.message)

    elif data == "back_main":
        bot.edit_message_text(WELCOME_TEXT, call.message.chat.id, call.message.message_id,
                               reply_markup=main_menu(), parse_mode='HTML')

    elif data == "add_group":
        bot.answer_callback_query(call.id, "🔗 Ссылка для добавления бота: https://t.me/FRK_Bot?startgroup=start",
                                   show_alert=True)


def build_profile(target_id):
    """Собирает текст и клавиатуру профиля пользователя (используется как
    при отправке нового сообщения, так и при редактировании существующего —
    например, при возврате из меню репутации)."""
    user_data = get_user(target_id)
    balance = user_data.get('balance', 0)
    deal_ids = user_data.get('deals', [])
    reputation = user_data.get('reputation', 0)
    registered_at = user_data.get('registered_at', datetime.now().isoformat())

    try:
        chat = bot.get_chat(target_id)
        username = chat.username or str(target_id)
    except telebot.apihelper.ApiException:
        username = str(target_id)
    username = _html.escape(username)

    deals = load_deals()
    completed = [deals[str(d)] for d in deal_ids if str(d) in deals and deals[str(d)].get('status') == 'completed']
    total_amount = sum(d.get('amount', 0) for d in completed)

    # Лайки/дизлайки репутации пока не реализованы как отдельная механика
    # голосования — показываем 0%, пока такая функция не добавлена.
    like_pct, dislike_pct = 0, 0

    reg_date = format_date_ru(registered_at)
    status = "Новичок" if reputation == 0 else "Проверенный"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Мои сделки", callback_data="my_deals"),
        types.InlineKeyboardButton("Репутация", callback_data=f"rep_menu_{target_id}"),
        types.InlineKeyboardButton("Назад", callback_data="back_main")
    )

    profile_text = (
        f"{tg_emoji('header', '👤')} @{username} (ID: {target_id})\n"
        f"<blockquote><b>"
        f"{tg_emoji('reputation', '🛡')} Репутации: {reputation} шт.\n"
        f"{tg_emoji('like', '👍')} · {like_pct}%\n"
        f"{tg_emoji('dislike', '👎')} · {dislike_pct}%"
        f"</b></blockquote>\n"
        f"<blockquote><b>"
        f"{tg_emoji('deals', '📝')} Сделок: {len(completed)} шт.\n"
        f"Общая сумма сделок {total_amount} {tg_emoji('total_amount', '💵')}"
        f"</b></blockquote>\n\n"
        f"<blockquote><b>"
        f"{tg_emoji('deposit', '💼')} Депозит: {balance} USDT"
        f"</b></blockquote>\n\n"
        f"<blockquote><b>"
        f"{tg_emoji('status', '⭐')} Статус: {status}"
        f"</b></blockquote>\n"
        f"<b>{tg_emoji('registered', '📅')} Зарегистрирован {reg_date}.</b>"
    )

    return profile_text, markup


def show_profile(target_chat_id, target_id):
    profile_text, markup = build_profile(target_id)
    bot.send_message(target_chat_id, profile_text, reply_markup=markup, parse_mode='HTML')


REPUTATION_FILTER_LABELS = {
    'all': 'Вся репутация',
    'pos': 'Положительная репутация',
    'neg': 'Отрицательная репутация',
    'lastpos': 'Последняя положительная репутация',
    'lastneg': 'Последняя отрицательная репутация',
}


def _profile_link(target_id):
    """Ссылка на профиль пользователя: по @username, если он есть,
    иначе по внутренней ссылке tg://user?id=<id>."""
    try:
        chat = bot.get_chat(target_id)
        username = chat.username
    except telebot.apihelper.ApiException:
        username = None
    if username:
        return f"https://t.me/{username}"
    return f"tg://user?id={target_id}"


def show_reputation_menu(chat_id, message_id, target_id):
    try:
        chat = bot.get_chat(target_id)
        username = chat.username or str(target_id)
    except telebot.apihelper.ApiException:
        username = str(target_id)
    username = _html.escape(username)

    text = f"{tg_emoji('reputation', '📋')} Какую репутацию @{username} вы хотите посмотреть?"

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("ВСЕ", callback_data=f"rep_all_{target_id}"))
    markup.row(
        types.InlineKeyboardButton("Положительные", callback_data=f"rep_pos_{target_id}"),
        types.InlineKeyboardButton("Отрицательные", callback_data=f"rep_neg_{target_id}"),
    )
    markup.row(
        types.InlineKeyboardButton("Последний положительный", callback_data=f"rep_lastpos_{target_id}"),
        types.InlineKeyboardButton("Последний отрицательный", callback_data=f"rep_lastneg_{target_id}"),
    )
    markup.add(types.InlineKeyboardButton("Назад", callback_data=f"rep_back_{target_id}"))

    bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')


def show_reputation_result(chat_id, message_id, target_id, filter_key):
    label = REPUTATION_FILTER_LABELS.get(filter_key, 'Репутация')
    link = _profile_link(target_id)

    text = f"{tg_emoji('reputation', '📋')} {label} <a href=\"{link}\">пользователя</a>."

    markup = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("◀ Назад", callback_data=f"rep_menu_{target_id}"))

    bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')


def show_my_deals(chat_id, message_id=None, page=1):
    """Показывает историю сделок пользователя в виде отдельной кнопки на
    каждую сделку (🔗 №<id> | <сумма> USDT) + строка пагинации снизу,
    как на макете."""
    user_data = get_user(chat_id)
    deal_ids = user_data.get('deals', [])
    deals = load_deals()

    # Только реально существующие сделки, самые новые — сверху
    valid_ids = [d for d in deal_ids if str(d) in deals]
    valid_ids = list(reversed(valid_ids))

    if not valid_ids:
        text = "У вас пока нет сделок."
        markup = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("Назад", callback_data="back_main"))
        if message_id:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
        else:
            bot.send_message(chat_id, text, reply_markup=markup)
        return

    total_pages = max(1, (len(valid_ids) + DEALS_PER_PAGE - 1) // DEALS_PER_PAGE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * DEALS_PER_PAGE
    page_ids = valid_ids[start:start + DEALS_PER_PAGE]

    markup = types.InlineKeyboardMarkup(row_width=1)
    for d_id in page_ids:
        d = deals[str(d_id)]
        amount_str = format_amount(d.get('amount', 0))
        btn_text = f"🔗 №{d_id} | {amount_str} USDT"
        if BOT_USERNAME:
            # URL-кнопка сама рисует стрелку "↗" справа, как на макете
            url = f"https://t.me/{BOT_USERNAME}?start=j-{d_id}"
            markup.add(types.InlineKeyboardButton(btn_text, url=url))
        else:
            markup.add(types.InlineKeyboardButton(btn_text, callback_data="noop"))

    nav_row = [types.InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
    if page > 1:
        nav_row.append(types.InlineKeyboardButton("◀", callback_data=f"mydeals_page_{page - 1}"))
    nav_row.append(types.InlineKeyboardButton(f"[{page}/{total_pages}]", callback_data="noop"))
    if page < total_pages:
        nav_row.append(types.InlineKeyboardButton("▶", callback_data=f"mydeals_page_{page + 1}"))
    markup.row(*nav_row)

    text = "<b>История ваших сделок:</b>"
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')


def status_label(status):
    return {
        'waiting_accept': 'ожидает принятия',
        'waiting_payment': 'ожидает оплаты',
        'in_escrow': 'в гарантии',
        'completed': 'завершена ✅',
        'disputed': 'спор ⚖️',
        'cancelled': 'отменена ❌',
        'declined': 'отклонена ❌',
    }.get(status, status)


# ============ КОШЕЛЁК ============
def show_wallet(user_id):
    balance = get_balance(user_id)

    wallet_text = (
        f"<b>💼 Ваш кошелёк</b>\n\n"
        f"<b>Баланс: {balance:.1f} {tg_emoji('total_amount', '💵')}</b>\n\n"
        f"<b>Кошелёк используется для оплаты сделок. Пополните баланс, чтобы начать.</b>"
    )

    # Строка с балансом сделана некликабельной кнопкой (а не <blockquote>),
    # т.к. HTML-цитата в Telegram всегда рисуется с полоской слева и
    # значком кавычек — а на макете нужна ровная "пилюля" без них.
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton(f"💰 Баланс: {balance:.1f} USDT", callback_data="noop"))
    markup.add(
        types.InlineKeyboardButton("➕ Пополнить", callback_data="wallet_deposit"),
        types.InlineKeyboardButton("➖ Вывести", callback_data="wallet_withdraw"),
    )
    markup.add(types.InlineKeyboardButton("Назад", callback_data="back_main"))

    bot.send_message(user_id, wallet_text, reply_markup=markup, parse_mode='HTML')


def show_deposit_menu(call):
    """Запрашивает у пользователя произвольную сумму пополнения текстом."""
    text = (
        f"Введите сумму пополнения в {tg_emoji('total_amount', '💵')} (мин. 1 {tg_emoji('total_amount', '💵')}).\n\n"
        "Комиссия: 5% (удерживается сверху)."
    )
    markup = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("Назад", callback_data="wallet")
    )
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='HTML')
    bot.register_next_step_handler(call.message, process_deposit_amount)


def process_deposit_amount(message):
    user_id = message.from_user.id
    text = message.text.strip()

    if text == 'Назад':
        start_command(message)
        return

    try:
        amount = float(text.replace(',', '.'))
        if amount < 1:
            raise ValueError
    except ValueError:
        bot.send_message(user_id, "❌ Введите корректную сумму (число, не меньше 1 USDT).")
        bot.register_next_step_handler(message, process_deposit_amount)
        return

    commission = amount * COMMISSION_RATE
    total_with_commission = amount + commission
    invoice_url = create_crypto_invoice(total_with_commission, user_id, amount)

    invoice_text = (
        f"<b>💳 Счёт на оплату\n\n"
        f"Сумма к оплате: {total_with_commission:.2f} {tg_emoji('total_amount', '💵')} "
        f"(с комиссией {commission:.2f} {tg_emoji('total_amount', '💵')})\n"
        f"После оплаты зачислится: {amount:.1f} {tg_emoji('total_amount', '💵')}\n\n"
        f"Счёт действителен 15 минут.</b>"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💳 Оплатить", url=invoice_url),
        types.InlineKeyboardButton("💼 Кошелёк", callback_data="wallet")
    )
    bot.send_message(user_id, invoice_text, reply_markup=markup, parse_mode='HTML')


def show_withdraw_menu(call):
    """Заглушка для вывода средств — в текущей версии функция вывода не
    реализована на бэкенде, поэтому просто уведомляем пользователя."""
    bot.answer_callback_query(call.id, "Минимальная сумма вывода — 1 USDT.", show_alert=True)


# ============ ПОИСК ПОЛЬЗОВАТЕЛЯ ============
def search_user(message):
    user_id = message.from_user.id
    query = message.text.strip()

    if query == 'Назад':
        start_command(message)
        return

    target_id = resolve_user_id(query)
    if target_id is None:
        bot.send_message(user_id, "❌ Пользователь не найден.")
        return

    show_profile(user_id, target_id)


# ============ СОЗДАНИЕ СДЕЛКИ ============
def create_open_deal(message, temp, user_key):
    """Контрагент ещё ни разу не писал боту, поэтому получить его ID через
    Bot API нельзя (по @username это работает только для публичных
    каналов/групп/ботов). Создаём сделку "открытой": ID второй стороны
    неизвестен до тех пор, пока она не перейдёт по инвайт-ссылке и не
    нажмёт /start — тогда она автоматически привяжется к сделке
    (см. show_deal_join_screen)."""
    user_id = message.from_user.id

    temp[user_key]['counterparty_id'] = None
    temp[user_key]['step'] = 'waiting_amount'
    save_temp(temp)

    bot.send_message(
        user_id,
        "⚠ Этот пользователь ещё не писал боту, поэтому найти его по ID/юзернейму "
        "невозможно. Сделка будет создана как «открытая»: после заполнения деталей "
        "вы получите ссылку-приглашение, которую нужно переслать контрагенту вручную. "
        "Как только он перейдёт по ней и нажмёт /start, сделка автоматически "
        "привяжется к нему.\n\n"
        "Отправьте сумму сделки.",
        reply_markup=types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("Назад", callback_data="back_main"))
    )
    bot.register_next_step_handler(message, deal_step_handler)


def deal_step_handler(message):
    user_id = message.from_user.id
    text = message.text.strip()

    if text == 'Назад':
        start_command(message)
        return

    temp = load_temp()
    user_key = str(user_id)

    if user_key not in temp:
        return

    step = temp[user_key].get('step', '')

    if step == 'waiting_counterparty':
        counterparty_id = resolve_user_id(text)
        if counterparty_id is None:
            # Пользователя нет в локальной базе (он ни разу не писал боту) —
            # создаём сделку "открытой": вместо конкретного ID сохраняем
            # приглашение по ссылке, которую инициатор сам перешлёт контрагенту.
            create_open_deal(message, temp, user_key)
            return
        if counterparty_id == user_id:
            bot.send_message(user_id, "❌ Нельзя создать сделку с самим собой.")
            bot.register_next_step_handler(message, deal_step_handler)
            return

        temp[user_key]['counterparty_id'] = counterparty_id
        temp[user_key]['step'] = 'waiting_amount'
        save_temp(temp)

        bot.send_message(
            user_id, "Отправьте сумму сделки (в USDT).",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Назад", callback_data="back_main"))
        )
        bot.register_next_step_handler(message, deal_step_handler)

    elif step == 'waiting_amount':
        try:
            amount = float(text.replace(',', '.'))
            if amount <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(user_id, "❌ Введите корректную сумму (число больше нуля).")
            bot.register_next_step_handler(message, deal_step_handler)
            return

        temp[user_key]['amount'] = amount
        temp[user_key]['step'] = 'waiting_description'
        save_temp(temp)

        bot.send_message(
            user_id, "Отправьте условия сделки.",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Назад", callback_data="back_main"))
        )
        bot.register_next_step_handler(message, deal_step_handler)

    elif step == 'waiting_description':
        desc = text.strip()
        if len(desc) < 75:
            bot.send_message(
                user_id,
                f"Минимальная длина описания — 75 символов. Сейчас: {len(desc)}.\n\n"
                f"Отправьте условия сделки подробнее."
            )
            bot.register_next_step_handler(message, deal_step_handler)
            return

        deal_type = temp[user_key].get('type', 'Покупаю')
        counterparty_id = temp[user_key].get('counterparty_id')
        amount = temp[user_key]['amount']
        is_open = counterparty_id is None

        # Инициатор ("Покупаю" -> он покупатель) / контрагент занимает вторую роль
        if deal_type == 'Покупаю':
            buyer_id, seller_id = user_id, counterparty_id
        else:
            buyer_id, seller_id = counterparty_id, user_id

        deal_id = generate_deal_id()
        deal_data = {
            'id': deal_id,
            'buyer': buyer_id,
            'seller': seller_id,
            'initiator': user_id,
            'type': deal_type,
            'amount': amount,
            'description': desc,
            'status': 'waiting_accept',
            'created_at': datetime.now().isoformat(),
            'accept_due': (datetime.now() + timedelta(minutes=ACCEPT_TIMEOUT_MIN)).isoformat(),
            'open': is_open,
        }

        deals = load_deals()
        deals[str(deal_id)] = deal_data
        save_deals(deals)

        add_deal_to_user(user_id, deal_id)
        if not is_open:
            add_deal_to_user(counterparty_id, deal_id)

        del temp[user_key]
        save_temp(temp)

        request_sent_text = f"<b>{tg_emoji('request_sent', '📨')} Запрос на сделку отправлен.</b>"
        bot.send_message(user_id, request_sent_text, parse_mode='HTML')

        role_label = "Покупатель" if user_id == buyer_id else "Продавец"
        initiator_display = _html.escape(display_username(user_id))
        desc_escaped = _html.escape(desc)

        if is_open:
            # Контрагент ещё не писал боту — сообщить ему напрямую невозможно,
            # поэтому даём инициатору ссылку-приглашение, которую он перешлёт сам.
            if not BOT_USERNAME:
                bot.send_message(
                    user_id,
                    "⚠ Не удалось создать ссылку-приглашение: не определён username бота. "
                    "Попробуйте создать сделку позже."
                )
                return

            join_url = f"https://t.me/{BOT_USERNAME}?start=j-{deal_id}"
            markup = types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("В меню", callback_data="back_main"))
            bot.send_message(
                user_id,
                f"<b>🔗 Сделка №{deal_id} создана как «открытая», т.к. контрагент ещё "
                f"не писал боту.\n\n"
                f"Перешлите второй стороне эту ссылку — как только пользователь перейдёт по "
                f"ней и нажмёт /start, сделка автоматически привяжется к ней:</b>\n"
                f"{join_url}",
                reply_markup=markup,
                parse_mode='HTML'
            )
            return

        # Обычная сделка — контрагент уже известен, отправляем ему приглашение
        counterparty = seller_id if user_id == buyer_id else buyer_id
        counterparty_display = _html.escape(display_username(counterparty))

        invite_text = (
            f"<b>{tg_emoji('invite_header', '👥')} @{counterparty_display}, вас пригласили в сделку на "
            f"{amount:.1f} {tg_emoji('total_amount', '💵')}.\n\n"
            f"{role_label}: @{initiator_display}\n\n"
            f"{tg_emoji('conditions', '📄')} Условия:\n{desc_escaped}</b>"
        )

        join_markup = types.InlineKeyboardMarkup(row_width=1)
        if BOT_USERNAME:
            join_url = f"https://t.me/{BOT_USERNAME}?start=j-{deal_id}"
            join_markup.add(types.InlineKeyboardButton("🚪 Присоединиться", url=join_url))
        else:
            # На случай, если username бота почему-то не определился при
            # старте — не ломаем поток, даём обычную кнопку внутри чата.
            join_markup.add(types.InlineKeyboardButton("🚪 Присоединиться", callback_data=f"accept_deal_{deal_id}"))

        try:
            bot.send_message(counterparty, invite_text, reply_markup=join_markup, parse_mode='HTML')
        except telebot.apihelper.ApiException:
            bot.send_message(
                user_id,
                "⚠ Не удалось уведомить вторую сторону (возможно, она заблокировала бота). "
                "Сделка создана, но может быть отменена по таймауту."
            )


def show_deal_join_screen(message, deal_id):
    """Экран, который видит приглашённая сторона после перехода по кнопке
    «Присоединиться» (диплинк /start j-<deal_id>). Сделка принимается сразу,
    без отдельного экрана подтверждения — если принимающий покупатель,
    тут же пытаемся списать средства.

    Для «открытых» сделок (contrparty_id был неизвестен на момент создания)
    первый, кто перейдёт по ссылке и не является инициатором, автоматически
    занимает недостающую роль (buyer или seller)."""
    user_id = message.from_user.id
    deals = load_deals()
    deal = deals.get(deal_id)

    if not deal:
        bot.send_message(user_id, "❌ Сделка не найдена или уже недоступна.", reply_markup=main_menu())
        return

    if deal.get('status') != 'waiting_accept':
        bot.send_message(user_id, "Эта сделка уже обработана или истёк срок присоединения.",
                          reply_markup=main_menu())
        return

    if deal.get('open'):
        if user_id == deal['initiator']:
            bot.send_message(user_id, "❌ Нельзя присоединиться к собственной сделке.", reply_markup=main_menu())
            return
        # Привязываем присоединившегося пользователя к недостающей роли
        if deal['buyer'] is None:
            deal['buyer'] = user_id
        else:
            deal['seller'] = user_id
        deal['open'] = False
        deals[deal_id] = deal
        save_deals(deals)
        add_deal_to_user(user_id, int(deal_id))
        process_deal_acceptance(deal_id, deal, user_id)
        return

    counterparty = deal['seller'] if deal['initiator'] == deal['buyer'] else deal['buyer']
    if user_id != counterparty:
        bot.send_message(user_id, "❌ Эта ссылка на сделку адресована не вам.", reply_markup=main_menu())
        return

    process_deal_acceptance(deal_id, deal, user_id)


def process_deal_acceptance(deal_id, deal, chat_id, message_id=None):
    """Общая логика принятия сделки: помечает её принятой и либо сразу
    пытается списать деньги (если принимающий — покупатель), либо просит
    покупателя оплатить (если принимающий — продавец). Работает как из
    диплинка (message_id=None -> новое сообщение), так и из callback-кнопки
    (message_id задан -> редактирует существующее сообщение)."""
    deal['status'] = 'waiting_payment'
    deal['payment_due'] = (datetime.now() + timedelta(minutes=PAYMENT_TIMEOUT_MIN)).isoformat()
    deals = load_deals()
    deals[deal_id] = deal
    save_deals(deals)

    if chat_id == deal['buyer']:
        # Принимающая сторона — покупатель: сразу пытаемся списать средства.
        try_pay_deal(chat_id, message_id, deal_id, deal)
    else:
        # Принимающая сторона — продавец: деньгами распоряжается покупатель,
        # ждём его оплату.
        text = f"✅ Вы приняли сделку №{deal_id}. Ожидаем оплату от покупателя."
        if message_id:
            bot.edit_message_text(text, chat_id, message_id)
        else:
            bot.send_message(chat_id, text)

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("Оплатить сделку", callback_data=f"pay_deal_{deal_id}"),
            types.InlineKeyboardButton("В меню", callback_data="back_main")
        )
        bot.send_message(
            deal['buyer'],
            f"✅ Сделка №{deal_id} принята второй стороной.\n"
            f"Оплатите сделку в течение {PAYMENT_TIMEOUT_MIN} минут, чтобы продолжить.",
            reply_markup=markup
        )


def try_pay_deal(chat_id, message_id, deal_id, deal):
    """Пытается списать средства покупателя в гарант.
    При успехе — сделка сразу уходит в эскроу и продавцу отправляется
    уведомление. При нехватке средств — показывает экран с просьбой
    пополнить кошелёк (карточка из макета). Если message_id задан —
    редактирует существующее сообщение, иначе отправляет новое."""
    amount = deal.get('amount', 0)
    buyer_id = deal['buyer']
    balance = get_balance(buyer_id)

    if balance >= amount:
        update_balance(buyer_id, -amount)  # средства замораживаются "в гаранте"
        deal['status'] = 'in_escrow'
        deal['paid_at'] = datetime.now().isoformat()
        deal['frozen_amount'] = amount
        deals = load_deals()
        deals[deal_id] = deal
        save_deals(deals)

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("✅ Подтвердить получение", callback_data=f"confirm_deal_{deal_id}"),
            types.InlineKeyboardButton("⚖️ Открыть спор", callback_data=f"dispute_deal_{deal_id}"),
            types.InlineKeyboardButton("В меню", callback_data="back_main")
        )
        text = (
            f"✅ Сделка №{deal_id} принята и оплачена, средства заморожены в гаранте.\n"
            f"Сумма: {amount:.2f} USDT\n\n"
            f"Когда получите товар/услугу — нажмите «Подтвердить получение», "
            f"чтобы деньги ушли продавцу. Если что-то не так — откройте спор."
        )
        if message_id:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
        else:
            bot.send_message(chat_id, text, reply_markup=markup)
        try:
            bot.send_message(
                deal['seller'],
                f"💰 Покупатель принял и оплатил сделку №{deal_id}. Средства в гаранте, "
                f"выполните свои обязательства по сделке."
            )
        except telebot.apihelper.ApiException:
            pass
        return True
    else:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("💼 Пополнить кошелёк", callback_data="wallet"),
            types.InlineKeyboardButton("💳 Оплатить сделку", callback_data=f"pay_deal_{deal_id}"),
        )
        insufficient_text = (
            f"<b>⚠️ Недостаточно средств для оплаты сделки №{deal_id}.\n\n"
            f"Нужно: {format_amount(amount)} {tg_emoji('total_amount', '💵')}\n"
            f"Доступно: {balance:.1f} {tg_emoji('total_amount', '💵')}\n\n"
            f"Пополните кошелёк и нажмите «Оплатить сделку». У вас есть {PAYMENT_TIMEOUT_MIN} минут.</b>"
        )
        if message_id:
            bot.edit_message_text(insufficient_text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')
        else:
            bot.send_message(chat_id, insufficient_text, reply_markup=markup, parse_mode='HTML')
        return False


def handle_deal_accept(call, accept):
    user_id = call.from_user.id
    deal_id = call.data.split('_')[-1]
    deals = load_deals()
    deal = deals.get(deal_id)

    if not deal:
        bot.answer_callback_query(call.id, "❌ Сделка не найдена", show_alert=True)
        return
    if deal.get('status') != 'waiting_accept':
        bot.answer_callback_query(call.id, "Эта сделка уже обработана.", show_alert=True)
        return

    # Принимать/отклонять может только та сторона, которой предложили сделку
    counterparty = deal['seller'] if deal['initiator'] == deal['buyer'] else deal['buyer']
    if user_id != counterparty:
        bot.answer_callback_query(call.id, "❌ Это предложение адресовано не вам.", show_alert=True)
        return

    if not accept:
        deal['status'] = 'declined'
        deals[deal_id] = deal
        save_deals(deals)
        bot.edit_message_text(f"❌ Сделка №{deal_id} отклонена.", call.message.chat.id, call.message.message_id)
        bot.send_message(deal['initiator'], f"❌ Вторая сторона отклонила сделку №{deal_id}.")
        return

    process_deal_acceptance(deal_id, deal, call.message.chat.id, call.message.message_id)


# ============ ОПЛАТА (СРЕДСТВА УХОДЯТ В ГАРАНТ, НЕ ПРОДАВЦУ) ============
def pay_deal(call):
    user_id = call.from_user.id
    deal_id = call.data.split('_')[2]

    deals = load_deals()
    deal = deals.get(deal_id)
    if not deal:
        bot.answer_callback_query(call.id, "❌ Сделка не найдена", show_alert=True)
        return

    # Платить может только покупатель этой сделки
    if user_id != deal['buyer']:
        bot.answer_callback_query(call.id, "❌ Оплатить эту сделку может только покупатель.", show_alert=True)
        return

    if deal.get('status') != 'waiting_payment':
        bot.answer_callback_query(call.id, "Эта сделка не ожидает оплаты.", show_alert=True)
        return

    try_pay_deal(call.message.chat.id, call.message.message_id, deal_id, deal)


# ============ ПОДТВЕРЖДЕНИЕ ПОЛУЧЕНИЯ (РАСКРЫТИЕ СРЕДСТВ ПРОДАВЦУ) ============
def confirm_deal(call):
    user_id = call.from_user.id
    deal_id = call.data.split('_')[-1]
    deals = load_deals()
    deal = deals.get(deal_id)

    if not deal:
        bot.answer_callback_query(call.id, "❌ Сделка не найдена", show_alert=True)
        return
    if user_id != deal['buyer']:
        bot.answer_callback_query(call.id, "❌ Подтвердить получение может только покупатель.", show_alert=True)
        return
    if deal.get('status') != 'in_escrow':
        bot.answer_callback_query(call.id, "Эту сделку нельзя подтвердить сейчас.", show_alert=True)
        return

    amount = deal.get('frozen_amount', deal.get('amount', 0))
    update_balance(deal['seller'], amount)  # раскрываем средства продавцу

    deal['status'] = 'completed'
    deal['completed_at'] = datetime.now().isoformat()
    deals[deal_id] = deal
    save_deals(deals)

    add_reputation(deal['buyer'], 1)
    add_reputation(deal['seller'], 1)

    bot.edit_message_text(f"✅ Сделка №{deal_id} завершена. Средства переведены продавцу.",
                           call.message.chat.id, call.message.message_id)
    bot.send_message(deal['seller'], f"✅ Покупатель подтвердил получение по сделке №{deal_id}. "
                                      f"Вам начислено {amount:.2f} USDT.")


def open_dispute(call):
    user_id = call.from_user.id
    deal_id = call.data.split('_')[-1]
    deals = load_deals()
    deal = deals.get(deal_id)

    if not deal:
        bot.answer_callback_query(call.id, "❌ Сделка не найдена", show_alert=True)
        return
    if user_id not in (deal['buyer'], deal['seller']):
        bot.answer_callback_query(call.id, "❌ Вы не участник этой сделки.", show_alert=True)
        return
    if deal.get('status') != 'in_escrow':
        bot.answer_callback_query(call.id, "Спор можно открыть только по сделке в гаранте.", show_alert=True)
        return

    deal['status'] = 'disputed'
    deal['dispute_opened_by'] = user_id
    deals[deal_id] = deal
    save_deals(deals)

    bot.edit_message_text(f"⚖️ По сделке №{deal_id} открыт спор. Ожидайте решения администратора.",
                           call.message.chat.id, call.message.message_id)

    other_party = deal['seller'] if user_id == deal['buyer'] else deal['buyer']
    try:
        bot.send_message(other_party, f"⚖️ По сделке №{deal_id} открыт спор второй стороной. "
                                       f"Администратор рассмотрит вопрос.")
    except telebot.apihelper.ApiException:
        pass

    for admin_id in load_admins():
        try:
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("В пользу продавца", callback_data=f"resolve_seller_{deal_id}"),
                types.InlineKeyboardButton("В пользу покупателя", callback_data=f"resolve_buyer_{deal_id}")
            )
            bot.send_message(
                admin_id,
                f"⚖️ Новый спор по сделке №{deal_id}\n"
                f"Сумма: {deal.get('amount', 0):.2f} USDT\n"
                f"Покупатель: {deal['buyer']}\nПродавец: {deal['seller']}\n"
                f"Условия: {deal.get('description', '')}\n"
                f"Спор открыл: {user_id}",
                reply_markup=markup
            )
        except telebot.apihelper.ApiException:
            pass


def show_disputes(call):
    user_id = call.from_user.id
    if user_id not in load_admins():
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return

    deals = load_deals()
    disputed = {k: v for k, v in deals.items() if v.get('status') == 'disputed'}
    if not disputed:
        bot.answer_callback_query(call.id, "Активных споров нет.", show_alert=True)
        return

    for deal_id, deal in disputed.items():
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("В пользу продавца", callback_data=f"resolve_seller_{deal_id}"),
            types.InlineKeyboardButton("В пользу покупателя", callback_data=f"resolve_buyer_{deal_id}")
        )
        bot.send_message(
            user_id,
            f"⚖️ Спор №{deal_id}\nСумма: {deal.get('amount', 0):.2f} USDT\n"
            f"Покупатель: {deal['buyer']}\nПродавец: {deal['seller']}\n"
            f"Условия: {deal.get('description', '')}",
            reply_markup=markup
        )


def resolve_dispute(call, favor):
    admin_id = call.from_user.id
    if admin_id not in load_admins():
        bot.answer_callback_query(call.id, "❌ Нет доступа", show_alert=True)
        return

    deal_id = call.data.split('_')[-1]
    deals = load_deals()
    deal = deals.get(deal_id)
    if not deal or deal.get('status') != 'disputed':
        bot.answer_callback_query(call.id, "Спор уже закрыт или сделка не найдена.", show_alert=True)
        return

    amount = deal.get('frozen_amount', deal.get('amount', 0))

    if favor == 'seller':
        update_balance(deal['seller'], amount)
        result_text = f"✅ Спор по сделке №{deal_id} решён в пользу продавца. Средства переведены продавцу."
    else:
        update_balance(deal['buyer'], amount)
        result_text = f"✅ Спор по сделке №{deal_id} решён в пользу покупателя. Средства возвращены покупателю."

    deal['status'] = 'completed'
    deal['completed_at'] = datetime.now().isoformat()
    deal['resolved_by_admin'] = admin_id
    deal['resolved_in_favor_of'] = favor
    deals[deal_id] = deal
    save_deals(deals)

    bot.edit_message_text(result_text, call.message.chat.id, call.message.message_id)
    for party in (deal['buyer'], deal['seller']):
        try:
            bot.send_message(party, result_text)
        except telebot.apihelper.ApiException:
            pass


# ============ АДМИН ФУНКЦИИ ============
def add_admin_process(message):
    user_id = message.from_user.id
    if user_id not in load_admins():
        return
    try:
        new_admin_id = int(message.text.strip())
    except ValueError:
        bot.send_message(user_id, "❌ Неверный формат ID. Введите число.")
        admin_tools(message)
        return

    admins = load_admins()
    if new_admin_id not in admins:
        admins.append(new_admin_id)
        save_admins(admins)
        bot.send_message(user_id, f"✅ Пользователь {new_admin_id} добавлен в админы.")
    else:
        bot.send_message(user_id, "❌ Этот пользователь уже админ.")
    admin_tools(message)


def remove_admin_process(message):
    user_id = message.from_user.id
    if user_id not in load_admins():
        return
    try:
        admin_id = int(message.text.strip())
    except ValueError:
        bot.send_message(user_id, "❌ Неверный формат ID. Введите число.")
        admin_tools(message)
        return

    admins = load_admins()
    if admin_id in admins:
        admins.remove(admin_id)
        save_admins(admins)
        bot.send_message(user_id, f"✅ Пользователь {admin_id} удален из админов.")
    else:
        bot.send_message(user_id, "❌ Этот пользователь не админ.")
    admin_tools(message)


def process_broadcast(message):
    user_id = message.from_user.id
    if user_id not in load_admins():
        return
    users = load_users()
    user_ids = [int(uid) for uid in users.keys()]

    if not user_ids:
        bot.send_message(user_id, "❌ Нет пользователей для рассылки.")
        return

    bot.send_message(user_id, f"📤 Начинаю рассылку для {len(user_ids)} пользователей...")

    success = 0
    for uid in user_ids:
        try:
            if message.photo:
                bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption)
            elif message.video:
                bot.send_video(uid, message.video.file_id, caption=message.caption)
            elif message.document:
                bot.send_document(uid, message.document.file_id, caption=message.caption)
            else:
                bot.send_message(uid, message.text)
            success += 1
            time.sleep(0.05)
        except telebot.apihelper.ApiException:
            pass

    bot.send_message(user_id, f"✅ Рассылка завершена! Отправлено: {success}/{len(user_ids)}")


# ============ CRYPTOBOT ИНТЕГРАЦИЯ ============
def create_crypto_invoice(total_amount, user_id, credited_amount):
    CRYPTO_TOKEN = ''  # замени на реальный токен из @CryptoBot

    url = 'https://pay.crypt.bot/api/createInvoice'
    headers = {
        'Crypto-Pay-API-Token': CRYPTO_TOKEN,
        'Content-Type': 'application/json'
    }
    data = {
        'asset': 'USDT',
        'amount': str(round(total_amount, 2)),
        'description': f'Пополнение баланса пользователя {user_id} на {credited_amount:.1f} USDT',
        'paid_btn_name': 'openBot',
        'paid_btn_url': f'https://t.me/FRK_Bot?start={user_id}'
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if result.get('ok'):
                return result['result']['pay_url']
    except requests.RequestException:
        pass

    # CRYPTO_TOKEN не настроен — реального счёта нет.
    # Возвращаем ссылку на бота вместо фейкового "инвойса", чтобы не создавать
    # у пользователя иллюзию оплаты, которая на деле не была принята.
    return 'https://t.me/CryptoBot'


# ============ ФОНОВЫЙ ПРОЦЕСС: ОТМЕНА ПРОСРОЧЕННЫХ СДЕЛОК ============
def timeout_checker_loop():
    while True:
        try:
            now = datetime.now()
            deals = load_deals()
            changed = False

            for deal_id, deal in deals.items():
                status = deal.get('status')

                if status == 'waiting_accept':
                    due = deal.get('accept_due')
                    if due and now > datetime.fromisoformat(due):
                        deal['status'] = 'cancelled'
                        changed = True
                        for party in (deal['buyer'], deal['seller']):
                            if party is None:
                                continue
                            try:
                                bot.send_message(party, f"⌛ Сделка №{deal_id} отменена: истекло время на принятие.")
                            except telebot.apihelper.ApiException:
                                pass

                elif status == 'waiting_payment':
                    due = deal.get('payment_due')
                    if due and now > datetime.fromisoformat(due):
                        deal['status'] = 'cancelled'
                        changed = True
                        for party in (deal['buyer'], deal['seller']):
                            if party is None:
                                continue
                            try:
                                bot.send_message(party, f"⌛ Сделка №{deal_id} отменена: истекло время на оплату.")
                            except telebot.apihelper.ApiException:
                                pass

            if changed:
                save_deals(deals)
        except Exception as e:  # фоновый поток не должен падать целиком из-за одной ошибки
            print(f"[timeout_checker_loop] ошибка: {e}")

        time.sleep(30)


# ============ ЗАПУСК ============
if __name__ == '__main__':
    init_files()

    try:
        BOT_USERNAME = bot.get_me().username
    except telebot.apihelper.ApiException as e:
        BOT_USERNAME = None
        print(f"⚠ Не удалось получить username бота (диплинки не будут работать): {e}")

    print("🤖 Бот запущен!")
    print(f"Username бота: @{BOT_USERNAME}")
    print(f"Админы: {load_admins()}")

    checker_thread = threading.Thread(target=timeout_checker_loop, daemon=True)
    checker_thread.start()

    bot.polling(none_stop=True, interval=0)