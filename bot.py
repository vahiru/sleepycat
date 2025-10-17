import logging
import re
from datetime import datetime, time, timedelta
import pytz
from collections import defaultdict

from telegram import BotCommand, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TELEGRAM_TOKEN
import database as db

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Constants ---
MUTE_PERMISSIONS = ChatPermissions(can_send_messages=False)
REMINDER_MINUTES = 15

# States for ConversationHandler
GET_HABIT_TIMES, GET_LEAVE_DAYS, GET_WEEKEND_OPTION, GET_HABIT_DURATION = range(4)


async def post_init(application: Application):
    commands = [
        BotCommand("init", "(仅群主) 初始化机器人各项设定"),
        BotCommand("settings", "查看当前群组的设定"),
        BotCommand("set", "设定一个普通的睡眠计划"),
        BotCommand("habit", "开启一个需要严格遵守的习惯计划"),
        BotCommand("plan", "查看我的睡眠计划"),
        BotCommand("remove", "移除我的睡眠计划"),
        BotCommand("leave", "为我的计划请假一天"),
        BotCommand("admin_remove", "(仅群主/管理员) 强制移除成员的计划"),
        BotCommand("help", "请求学生会的帮助"),
    ]
    await application.bot.set_my_commands(commands)

# --- Scheduler Job ---
async def check_schedules(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    schedules = db.get_all_schedules()
    schedules_by_chat = defaultdict(list)
    for schedule in schedules:
        schedules_by_chat[schedule['chat_id']].append(schedule)

    for chat_id, chat_schedules in schedules_by_chat.items():
        group_settings = db.get_group_settings(chat_id)
        try:
            group_tz = pytz.timezone(group_settings['timezone'])
        except pytz.UnknownTimeZoneError:
            continue

        now = datetime.now(group_tz)
        today_date_str = now.strftime('%Y-%m-%d')
        is_weekend = now.weekday() >= 5

        for schedule in chat_schedules:
            user_id = schedule['user_id']
            user_name = schedule['user_name']

            if schedule['leave_until'] == today_date_str:
                continue
            if schedule['plan_type'] == 'habit' and schedule['habit_exempt_weekends'] and is_weekend:
                continue
            if schedule['plan_type'] == 'habit' and schedule['habit_end_date'] and today_date_str > schedule['habit_end_date']:
                db.set_schedule(user_id, chat_id, user_name, schedule['sleep_time'], schedule['wake_time']) # Convert habit to normal plan
                continue

            try:
                sleep_time_obj = datetime.strptime(schedule['sleep_time'], '%H:%M').time()
            except (ValueError, TypeError):
                continue

            # Reminder Logic
            reminder_time = (datetime.combine(now.date(), sleep_time_obj) - timedelta(minutes=REMINDER_MINUTES)).time()
            if now.strftime('%H:%M') == reminder_time.strftime('%H:%M') and schedule['reminder_sent_date'] != today_date_str:
                try:
                    await bot.send_message(chat_id, f"@{user_name}，哥哥，还有 {REMINDER_MINUTES} 分钟就到休息时间了哦，该准备了。")
                    db.update_reminder_sent(user_id, today_date_str)
                except Exception as e:
                    logger.error(f"Reminder failed for {user_id}: {e}")

            # Mute Logic
            if now.strftime('%H:%M') == schedule['sleep_time']:
                try:
                    wake_time_obj = datetime.strptime(schedule['wake_time'], '%H:%M').time()
                    # Create a timezone-aware datetime for the wake-up time
                    wake_datetime = now.replace(hour=wake_time_obj.hour, minute=wake_time_obj.minute, second=0, microsecond=0)
                    # If the calculated wake time is in the past (relative to now), it must be for the next day.
                    if wake_datetime <= now:
                        wake_datetime += timedelta(days=1)

                    # Telegram API considers bans < 30s as permanent. Check for this case.
                    if (wake_datetime - now) < timedelta(seconds=30):
                        logger.warning(f"Mute duration for {user_id} is too short (< 30s). Skipping mute to avoid permanent ban.")
                        await bot.send_message(chat_id, f"@{user_name}，哥哥，你设定的时间间隔太短了，妃爱没法帮你禁言呢。")
                        continue

                    await bot.restrict_chat_member(chat_id, user_id, permissions=MUTE_PERMISSIONS, until_date=wake_datetime)
                    logger.info(f"Muted user {user_id} in chat {chat_id} until {wake_datetime}")
                    await bot.send_message(chat_id, f"时间到了。为了哥哥的健康，从现在开始到 {wake_datetime.strftime('%H:%M')}，@{user_name} 就由妃爱来保护了。晚安，哥哥。")
                except Exception as e:
                    logger.error(f"Native mute failed for {user_id}: {e}")

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f'哥哥……是你吗？我是妃爱。以后你的作息就由我来管理了。\n啊，不过在这之前，得先让这个群的群主用 /init 命令做一下初始设置才行。')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "哥哥需要帮助吗？……真拿你没办法呢。\n这是妃爱为你准备的命令列表，要记好哦：\n\n"
        "/init <时区> - (群主用) 为本群设定正确的时区\n"
        "/settings - 查看本群的各项设定\n"
        "/set HH:MM HH:MM - 设定或修改你的普通计划\n"
        "/habit - 开启一个严格的习惯养成计划\n"
        "/plan - 查看你当前的计划详情\n"
        "/remove - 移除你的普通计划\n"
        "/leave - 为你的计划请假一天\n"
        "/admin_remove - (群主/管理员用) 回复某人消息以移除其计划"
    )
    await update.message.reply_text(help_text)

async def init_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("哥哥，这个命令是用来设定别人的东西的，在群里用哦。")
        return
    administrators = await context.bot.get_chat_administrators(chat.id)
    if not any(admin.status == 'creator' and admin.user.id == user.id for admin in administrators):
        await update.message.reply_text("嗯？这种事应该让群主来做吧。")
        return
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("用法不对啦。应该是 /init <时区>，例如: /init Asia/Shanghai")
        return
    tz_str = context.args[0]
    if tz_str not in pytz.all_timezones:
        await update.message.reply_text(f"'{tz_str}'…这是什么？妃爱不认识呢。从列表里选个正确的，别给哥哥添麻烦。")
        return
    db.set_group_timezone(chat.id, tz_str)
    await update.message.reply_text(f"好了好了，设定完了。这个群的时区现在是 {tz_str}。")

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("哥哥，我们之间才没有什么设定呢。")
        return
    settings = db.get_group_settings(chat.id)
    await update.message.reply_text(f"哥哥是想看这个群的设定吗？嗯……好像只有这个呢。\n- 时区: {settings['timezone']}")

async def leave_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("哥哥是想休息一天吗？好的，妃爱记下了。")
        return

    settings = db.get_group_settings(chat.id)
    group_tz = pytz.timezone(settings['timezone'])
    today_str = datetime.now(group_tz).strftime('%Y-%m-%d')
    result = db.apply_leave_day(user.id, today_str)
    if result == 'success_normal':
        await update.message.reply_text(f"好的，哥哥。今天就好好休息吧。")
    elif result == 'success_habit':
        schedule = db.get_schedule(user.id)
        remaining_days = schedule['habit_total_leave_days'] - schedule['habit_used_leave_days']
        await update.message.reply_text(f"……真拿哥哥没办法呢。仅此一次哦。你的习惯计划就为你暂停一天，还剩下 {remaining_days} 天假。")
    elif result == 'no_days_left':
        await update.message.reply_text("不行哦哥哥，你的假期已经用完了，不可以再偷懒了。")
    elif result == 'no_plan':
        await update.message.reply_text("哥哥还没有设定计划，所以不需要请假哦。")

async def set_sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("哥哥要设定计划吗？请在这里告诉我你的就寝和起床时间。")
        return
    schedule = db.get_schedule(user.id)
    if schedule and schedule['plan_type'] == 'habit':
        await update.message.reply_text("不行哦哥哥，严格的习惯是不可以随便更改的。")
        return
    administrators = await context.bot.get_chat_administrators(chat.id)
    if any(admin.status == 'creator' and admin.user.id == user.id for admin in administrators):
        await update.message.reply_text("……群主？妃爱只关心哥哥的作息，其他人的与我无关。")
        return
    if not context.args or len(context.args) != 2:
        await update.message.reply_text("哥哥，用法是 /set HH:MM HH:MM 哦。")
        return
    sleep_time_str, wake_time_str = context.args
    time_pattern = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')
    if not time_pattern.match(sleep_time_str) or not time_pattern.match(wake_time_str):
        await update.message.reply_text("时间格式应该是 HH:MM，请检查一下。")
        return
    db.set_schedule(user.id, chat.id, user.first_name, sleep_time_str, wake_time_str)
    await update.message.reply_text(f"好的，你的普通计划已更新。\n睡觉时间: {sleep_time_str}\n起床时间: {wake_time_str}")

async def my_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    schedule = db.get_schedule(user.id)
    if not schedule:
        await update.message.reply_text("哥哥还没有告诉妃爱你的计划哦。")
        return
    plan_type = "习惯" if schedule['plan_type'] == 'habit' else "普通"
    text = f"哥哥，这是你现在的计划 ({plan_type})：\n- 就寝时间: {schedule['sleep_time']}\n- 起床时间: {schedule['wake_time']}"
    if schedule['plan_type'] == 'habit':
        exempt_weekends = "是" if schedule['habit_exempt_weekends'] else "否"
        remaining_leave = schedule['habit_total_leave_days'] - schedule['habit_used_leave_days']
        end_date_str = f"至 {schedule['habit_end_date']}" if schedule['habit_end_date'] else "(永久)"
        text += f"\n- 状态: 严格遵守中 {end_date_str}\n- 周末豁免: {exempt_weekends}\n- 剩余请假天数: {remaining_leave}\n\n要好好加油哦，哥哥。"
    await update.message.reply_text(text)

async def remove_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    schedule = db.get_schedule(user.id)
    if schedule and schedule['plan_type'] == 'habit':
        await update.message.reply_text("不行，说好了要严格遵守的，哥哥不许耍赖。")
        return
    rows_deleted = db.remove_schedule(user.id)
    if rows_deleted > 0:
        await update.message.reply_text("嗯，哥哥的计划已经移除了。")
    else:
        await update.message.reply_text("哥哥本来就没有设定计划呀。")

async def admin_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("此命令仅在群组中可用。")
        return
    
    settings = db.get_group_settings(chat.id)
    administrators = await context.bot.get_chat_administrators(chat.id)
    is_owner = any(admin.status == 'creator' and admin.user.id == user.id for admin in administrators)
    is_admin = any(admin.user.id == user.id for admin in administrators)

    if not is_owner and not (is_admin and settings['admin_can_break_habit']):
        await update.message.reply_text("只有哥哥指定的管理员才能命令我。")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("要移除谁的计划？请回复那个人的消息。")
        return

    target_user = update.message.reply_to_message.from_user
    rows_deleted = db.remove_schedule(target_user.id)
    if rows_deleted > 0:
        await update.message.reply_text(f"嗯，{target_user.first_name} 的计划被移除了。就这样。")
    else:
        await update.message.reply_text(f"……这个人本来就没有计划。")


async def temp_mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if chat.type == 'private':
        await update.message.reply_text("此命令仅在群组中可用。")
        return

    # Check if user is an admin or owner
    administrators = await context.bot.get_chat_administrators(chat.id)
    if not any(admin.user.id == user.id for admin in administrators):
        await update.message.reply_text("只有哥哥指定的管理员才能命令我。")
        return

    # Check if it's a reply
    if not update.message.reply_to_message:
        await update.message.reply_text("要禁言谁？请回复那个人的消息。")
        return

    # Check for arguments
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("用法: /tmute <时长> (例如: 5m, 1d, 30s)")
        return

    # Parse duration
    duration_str = context.args[0]
    match = re.match(r'^(\d+)([dms])$', duration_str.lower())

    if not match:
        await update.message.reply_text("时长格式错误。请使用 d (天), m (分钟), 或 s (秒)。")
        return

    value = int(match.group(1))
    unit = match.group(2)
    delta = timedelta()

    if unit == 'd':
        delta = timedelta(days=value)
    elif unit == 'm':
        delta = timedelta(minutes=value)
    elif unit == 's':
        delta = timedelta(seconds=value)

    # Validate duration and execute mute
    if timedelta(seconds=30) <= delta <= timedelta(days=366):
        try:
            group_settings = db.get_group_settings(chat.id)
            group_tz = pytz.timezone(group_settings['timezone'])
            now = datetime.now(group_tz)
            unmute_date = now + delta

            target_user = update.message.reply_to_message.from_user
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=target_user.id,
                permissions=MUTE_PERMISSIONS,
                until_date=unmute_date
            )
            await update.message.reply_text(f"来，{target_user.first_name}哥哥，张嘴，妃爱会在 {value}{unit} 之后把它拿下来的。")
        except Exception as e:
            logger.error(f"Failed to temp mute {target_user.id}: {e}")
            await update.message.reply_text(f"禁言失败了。")
    else:
        await update.message.reply_text("禁言时长必须在30秒到366天之间。")


# --- Habit Conversation Handlers ---
async def start_habit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("要和妃爱一起养成好习惯吗？这需要哥哥的决心哦。\n首先，请设定你的就寝与起床时间 (格式: HH:MM HH:MM)。\n\n随时可以输入 /cancel 放弃，但妃爱会失望的。")
    return GET_HABIT_TIMES

async def get_habit_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    times = update.message.text.split()
    if len(times) != 2 or not all(re.match(r'^([01]\d|2[0-3]):([0-5]\d)$', t) for t in times):
        await update.message.reply_text("哥哥，时间格式不对哦，是 HH:MM HH:MM 这样。再试一次吧。")
        return GET_HABIT_TIMES
    context.user_data['habit_times'] = times
    keyboard = [[InlineKeyboardButton("7天", callback_data='7'), InlineKeyboardButton("21天", callback_data='21'), InlineKeyboardButton("30天", callback_data='30'), InlineKeyboardButton("永久", callback_data='0')]]
    await update.message.reply_text("第二步：哥哥计划让这个习惯持续多久呢？", reply_markup=InlineKeyboardMarkup(keyboard))
    return GET_HABIT_DURATION

async def get_habit_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['habit_duration'] = int(query.data)
    keyboard = [[InlineKeyboardButton("0天", callback_data='0'), InlineKeyboardButton("1天", callback_data='1'), InlineKeyboardButton("3天", callback_data='3'), InlineKeyboardButton("5天", callback_data='5')]]
    await query.edit_message_text("第三步：嗯……作为特殊照顾，妃爱允许哥哥设置几天的请假额度呢？", reply_markup=InlineKeyboardMarkup(keyboard))
    return GET_LEAVE_DAYS

async def get_leave_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['habit_leave_days'] = int(query.data)
    keyboard = [[InlineKeyboardButton("是", callback_data='1'), InlineKeyboardButton("否", callback_data='0')]]
    await query.edit_message_text("最后一步：哥哥周末需要休息一下吗？（是否豁免周末计划）", reply_markup=InlineKeyboardMarkup(keyboard))
    return GET_WEEKEND_OPTION

async def get_weekend_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = context.user_data
    user = query.from_user
    chat = query.message.chat
    sleep_time, wake_time = user_data['habit_times']
    duration = user_data['habit_duration']
    total_leave = user_data['habit_leave_days']
    exempt_weekends = bool(int(query.data))
    end_date_str = (datetime.now(pytz.timezone(db.get_group_settings(chat.id)['timezone'])) + timedelta(days=duration)).strftime('%Y-%m-%d') if duration > 0 else None
    db.set_full_habit_schedule(user.id, chat.id, user.first_name, sleep_time, wake_time, total_leave, exempt_weekends, end_date_str)
    exempt_text = "是" if exempt_weekends else "否"
    duration_text = f"{duration}天" if duration > 0 else "永久"
    await query.edit_message_text(
        f"太好了，哥哥。我们的约定成立了哦。\n\n"
        f"- 就寝时间: {sleep_time}\n"
        f"- 起床时间: {wake_time}\n"
        f"- 持续时间: {duration_text}\n"
        f"- 请假额度: {total_leave}天\n"
        f"- 周末豁免: {exempt_text}\n\n"
        f"妃爱会一直陪着哥哥的，一起加油吧！"
    )
    user_data.clear()
    return ConversationHandler.END

async def cancel_habit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("……好吧。既然哥哥这么决定了。")
    context.user_data.clear()
    return ConversationHandler.END

def main() -> None:
    db.init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    habit_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('habit', start_habit)],
        states={
            GET_HABIT_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_habit_times)],
            GET_HABIT_DURATION: [CallbackQueryHandler(get_habit_duration)],
            GET_LEAVE_DAYS: [CallbackQueryHandler(get_leave_days)],
            GET_WEEKEND_OPTION: [CallbackQueryHandler(get_weekend_option)],
        },
        fallbacks=[CommandHandler('cancel', cancel_habit)],
    )
    application.add_handler(habit_conv_handler)
    application.add_handler(CommandHandler(("start",), start))
    application.add_handler(CommandHandler(("help",), help_command))
    application.add_handler(CommandHandler(("init",), init_command))
    application.add_handler(CommandHandler(("settings",), settings_command))
    application.add_handler(CommandHandler(("leave",), leave_command))
    application.add_handler(CommandHandler(("set", "set_sleep"), set_sleep))
    application.add_handler(CommandHandler(("plan", "my_schedule"), my_schedule))
    application.add_handler(CommandHandler(("remove", "remove_schedule"), remove_schedule_command))
    application.add_handler(CommandHandler(("admin_remove",), admin_remove_command))
    application.add_handler(CommandHandler("tmute", temp_mute_command))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_schedules, 'interval', minutes=1, kwargs={"context": application})
    scheduler.start()
    logger.info("Bot started.")
    application.run_polling()

if __name__ == "__main__":
    main()
