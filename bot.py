import logging
import re
from datetime import datetime
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
UNMUTE_PERMISSIONS = ChatPermissions(can_send_messages=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True)

# States for ConversationHandler
GET_HABIT_TIMES, GET_LEAVE_DAYS, GET_WEEKEND_OPTION = range(3)


async def post_init(application: Application):
    """Post-initialization function to set bot commands."""
    commands = [
        BotCommand("init", "(仅群主) 初始化机器人"),
        BotCommand("settings", "查看当前群组设定"),
        BotCommand("set", "设置普通睡眠计划"),
        BotCommand("habit", "开启一个全新的习惯计划"),
        BotCommand("plan", "查看我的睡眠计划"),
        BotCommand("remove", "移除我的睡眠计划"),
        BotCommand("leave", "请假一天"),
        BotCommand("help", "获取帮助信息"),
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
        current_time_str = now.strftime('%H:%M')
        today_date_str = now.strftime('%Y-%m-%d')
        is_weekend = now.weekday() >= 5

        for schedule in chat_schedules:
            if schedule['leave_until'] == today_date_str:
                continue
            
            if schedule['plan_type'] == 'habit' and schedule['habit_exempt_weekends'] and is_weekend:
                continue

            user_id, sleep_time, wake_time, is_muted = schedule['user_id'], schedule['sleep_time'], schedule['wake_time'], bool(schedule['is_muted'])

            if current_time_str == sleep_time and not is_muted:
                try:
                    await bot.restrict_chat_member(chat_id, user_id, permissions=MUTE_PERMISSIONS)
                    db.update_mute_status(user_id, True)
                    await bot.send_message(chat_id, f"一位群友到点睡觉，已被禁言。晚安！")
                except Exception as e:
                    logger.error(f"Mute failed for {user_id}: {e}")

            elif current_time_str == wake_time and is_muted:
                try:
                    await bot.restrict_chat_member(chat_id, user_id, permissions=UNMUTE_PERMISSIONS)
                    db.update_mute_status(user_id, False)
                    await bot.send_message(chat_id, f"一位群友睡醒了，已解除禁言。早！")
                except Exception as e:
                    logger.error(f"Unmute failed for {user_id}: {e}")

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f'你好, {update.effective_user.first_name}! 我是睡眠管理机器人。\n请让群主使用 /init 命令来初始化本群设置。')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "你可以使用以下命令:\n\n"
        "/init <时区> - (仅群主) 设置群组时区\n"
        "/settings - 查看当前群组设定\n"
        "/set HH:MM HH:MM - 设置或修改你的普通睡眠计划\n"
        "/habit - 开始设置一个锁定的习惯计划\n"
        "/plan - 查看你当前的睡眠计划\n"
        "/remove - 移除你的普通睡眠计划\n"
        "/leave - 请假一天\n"
        "/help - 显示这条帮助信息"
    )
    await update.message.reply_text(help_text)

async def init_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("请在群组中运行此命令。")
        return
    administrators = await context.bot.get_chat_administrators(chat.id)
    if not any(admin.status == 'creator' and admin.user.id == user.id for admin in administrators):
        await update.message.reply_text("抱歉, 只有群主才能使用此命令。")
        return
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("格式错误. 请提供一个时区. \n例如: /init Asia/Shanghai")
        return
    tz_str = context.args[0]
    if tz_str not in pytz.all_timezones:
        await update.message.reply_text(f"错误: '{tz_str}' 不是一个有效的时区. \n你可以从这里查找: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones")
        return
    db.set_group_timezone(chat.id, tz_str)
    await update.message.reply_text(f"群组时区已成功设置为: {tz_str}")

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("请在群组中运行此命令。")
        return
    settings = db.get_group_settings(chat.id)
    await update.message.reply_text(f"当前群组设置:\n- 时区: {settings['timezone']}")

async def leave_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("请在群组中运行此命令。")
        return

    settings = db.get_group_settings(chat.id)
    group_tz = pytz.timezone(settings['timezone'])
    today_str = datetime.now(group_tz).strftime('%Y-%m-%d')
    result = db.apply_leave_day(user.id, today_str)

    if result == 'success_normal':
        await update.message.reply_text(f"好的, {user.first_name}. 你今天的普通计划已暂停。")
    elif result == 'success_habit':
        schedule = db.get_schedule(user.id)
        remaining_days = schedule['habit_total_leave_days'] - schedule['habit_used_leave_days']
        await update.message.reply_text(f"习惯计划请假成功！你还剩余 {remaining_days} 天可请假。")
    elif result == 'no_days_left':
        await update.message.reply_text("习惯计划请假失败：你的请假天数已用完！")
    elif result == 'no_plan':
        await update.message.reply_text("你还没有设置睡眠计划，无需请假。")

async def set_sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("请在群组中设置睡眠计划。")
        return
    schedule = db.get_schedule(user.id)
    if schedule and schedule['plan_type'] == 'habit':
        await update.message.reply_text("你正在执行一个习惯计划，无法被修改。")
        return
    administrators = await context.bot.get_chat_administrators(chat.id)
    if any(admin.status == 'creator' and admin.user.id == user.id for admin in administrators):
        await update.message.reply_text("你是群主，不能被禁言。")
        return
    if not context.args or len(context.args) != 2:
        await update.message.reply_text("格式错误. 请使用: /set <睡觉时间> <起床时间>")
        return
    sleep_time_str, wake_time_str = context.args
    time_pattern = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')
    if not time_pattern.match(sleep_time_str) or not time_pattern.match(wake_time_str):
        await update.message.reply_text("时间格式不正确. 请使用 HH:MM 格式。")
        return
    db.set_schedule(user.id, chat.id, sleep_time_str, wake_time_str)
    await update.message.reply_text(f"普通计划已更新!\n用户: {user.first_name}\n睡觉时间: {sleep_time_str}\n起床时间: {wake_time_str}")

async def my_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    schedule = db.get_schedule(user.id)
    if not schedule:
        await update.message.reply_text("你还没有设置睡眠计划。")
        return
    plan_type = "习惯" if schedule['plan_type'] == 'habit' else "普通"
    text = f"你当前的计划 ({plan_type}):\n- 睡觉时间: {schedule['sleep_time']}\n- 起床时间: {schedule['wake_time']}"
    if schedule['plan_type'] == 'habit':
        exempt_weekends = "是" if schedule['habit_exempt_weekends'] else "否"
        remaining_leave = schedule['habit_total_leave_days'] - schedule['habit_used_leave_days']
        text += f"\n- 习惯模式锁定中\n- 周末豁免: {exempt_weekends}\n- 剩余请假天数: {remaining_leave}"
    await update.message.reply_text(text)

async def remove_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    schedule = db.get_schedule(user.id)
    if schedule and schedule['plan_type'] == 'habit':
        await update.message.reply_text("你正在执行一个习惯计划，无法被移除。")
        return
    rows_deleted = db.remove_schedule(user.id)
    if rows_deleted > 0:
        await update.message.reply_text("你的睡眠计划已被移除。")
    else:
        await update.message.reply_text("你还没有设置睡眠计划，无需移除。")

# --- Habit Conversation Handlers ---
async def start_habit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "你正准备开启一个【习惯计划】。\n"
        "请输入你的【睡觉时间】和【起床时间】 (例如: 23:00 07:00)。\n\n"
        "随时输入 /cancel 可以取消。"
    )
    return GET_HABIT_TIMES

async def get_habit_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    times = update.message.text.split()
    if len(times) != 2 or not all(re.match(r'^([01]\d|2[0-3]):([0-5]\d)$', t) for t in times):
        await update.message.reply_text("格式错误，请重新输入时间 (例如: 23:00 07:00)，或 /cancel 取消。")
        return GET_HABIT_TIMES
    context.user_data['habit_times'] = times
    keyboard = [[InlineKeyboardButton("0天", callback_data='0'), InlineKeyboardButton("1天", callback_data='1'), InlineKeyboardButton("3天", callback_data='3'), InlineKeyboardButton("5天", callback_data='5')]]
    await update.message.reply_text("第二步：请选择这个习惯计划包含的【可请假天数】。", reply_markup=InlineKeyboardMarkup(keyboard))
    return GET_LEAVE_DAYS

async def get_leave_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['habit_leave_days'] = int(query.data)
    keyboard = [[InlineKeyboardButton("是", callback_data='1'), InlineKeyboardButton("否", callback_data='0')]]
    await query.edit_message_text("最后一步：这个习惯计划是否【豁免周末】（周六日不执行）？", reply_markup=InlineKeyboardMarkup(keyboard))
    return GET_WEEKEND_OPTION

async def get_weekend_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = context.user_data
    user = query.from_user
    chat = query.message.chat
    sleep_time, wake_time = user_data['habit_times']
    total_leave = user_data['habit_leave_days']
    exempt_weekends = bool(int(query.data))
    db.set_full_habit_schedule(user.id, chat.id, sleep_time, wake_time, total_leave, exempt_weekends)
    exempt_text = "是" if exempt_weekends else "否"
    await query.edit_message_text(
        f"太棒了！你的【习惯计划】已设定并锁定。\n\n"
        f"- 睡觉时间: {sleep_time}\n"
        f"- 起床时间: {wake_time}\n"
        f"- 可请假天数: {total_leave}天\n"
        f"- 周末豁免: {exempt_text}\n\n"
        f"请加油！"
    )
    user_data.clear()
    return ConversationHandler.END

async def cancel_habit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("习惯计划设置已取消。")
    context.user_data.clear()
    return ConversationHandler.END

def main() -> None:
    db.init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    habit_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('habit', start_habit)],
        states={
            GET_HABIT_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_habit_times)],
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
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_schedules, 'interval', minutes=1, kwargs={"context": application})
    scheduler.start()
    logger.info("Bot started.")
    application.run_polling()

if __name__ == "__main__":
    main()
