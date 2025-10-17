import sqlite3

def init_db():
    """Initializes all database tables."""
    _create_schedules_table()
    _create_group_settings_table()

def _create_schedules_table():
    """Creates the user schedules table with all fields for normal and habit plans."""
    conn = sqlite3.connect('sleepybot.db')
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            sleep_time TEXT NOT NULL,
            wake_time TEXT NOT NULL,
            is_muted INTEGER DEFAULT 0,
            plan_type TEXT DEFAULT 'normal',
            leave_until TEXT,
            habit_total_leave_days INTEGER DEFAULT 0,
            habit_used_leave_days INTEGER DEFAULT 0,
            habit_exempt_weekends INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def _create_group_settings_table():
    """Creates the group settings table if it doesn't exist."""
    conn = sqlite3.connect('sleepybot.db')
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS group_settings (
            chat_id INTEGER PRIMARY KEY,
            timezone TEXT DEFAULT 'UTC',
            max_leave_days INTEGER DEFAULT 3,
            admin_can_break_habit INTEGER DEFAULT 0,
            admin_can_set_for_others INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def get_group_settings(chat_id):
    """Retrieves settings for a specific group, creating them if they don't exist."""
    conn = sqlite3.connect('sleepybot.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,))
    settings = cursor.fetchone()
    if not settings:
        cursor.execute("INSERT OR IGNORE INTO group_settings (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        cursor.execute("SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,))
        settings = cursor.fetchone()
    conn.close()
    return settings

def set_group_timezone(chat_id, timezone_str):
    """Sets the timezone for a specific group."""
    conn = sqlite3.connect('sleepybot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO group_settings (chat_id, timezone) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET timezone=excluded.timezone", (chat_id, timezone_str))
    conn.commit()
    conn.close()

# --- Schedule Functions ---

def set_schedule(user_id, chat_id, sleep_time, wake_time):
    """Saves or updates a user's normal sleep schedule."""
    conn = sqlite3.connect('sleepybot.db')
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO schedules (user_id, chat_id, sleep_time, wake_time, plan_type, habit_total_leave_days, habit_used_leave_days, habit_exempt_weekends)
        VALUES (?, ?, ?, ?, 'normal', 0, 0, 0)
    """, (user_id, chat_id, sleep_time, wake_time))
    conn.commit()
    conn.close()

def set_full_habit_schedule(user_id, chat_id, sleep_time, wake_time, total_leave, exempt_weekends):
    """Saves or updates a user's full habit sleep schedule."""
    conn = sqlite3.connect('sleepybot.db')
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO schedules (user_id, chat_id, sleep_time, wake_time, plan_type, habit_total_leave_days, habit_used_leave_days, habit_exempt_weekends)
        VALUES (?, ?, ?, ?, 'habit', ?, 0, ?)
    """, (user_id, chat_id, sleep_time, wake_time, total_leave, exempt_weekends))
    conn.commit()
    conn.close()

def get_schedule(user_id):
    """Retrieves a user's full schedule, including plan type."""
    conn = sqlite3.connect('sleepybot.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM schedules WHERE user_id = ?", (user_id,))
    schedule = cursor.fetchone()
    conn.close()
    return schedule

def get_all_schedules():
    """Retrieves all sleep schedules from the database."""
    conn = sqlite3.connect('sleepybot.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM schedules")
    schedules = cursor.fetchall()
    conn.close()
    return schedules

def remove_schedule(user_id):
    """Removes a user's sleep schedule from the database."""
    conn = sqlite3.connect('sleepybot.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM schedules WHERE user_id = ?", (user_id,))
    rows_deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return rows_deleted

def update_mute_status(user_id, is_muted: bool):
    """Updates the mute status for a specific user."""
    conn = sqlite3.connect('sleepybot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE schedules SET is_muted = ? WHERE user_id = ?", (int(is_muted), user_id))
    conn.commit()
    conn.close()

def apply_leave_day(user_id, date_str):
    """Applies a leave day for a user, checking for habit plan rules."""
    conn = sqlite3.connect('sleepybot.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM schedules WHERE user_id = ?", (user_id,))
    schedule = cursor.fetchone()
    if not schedule:
        return 'no_plan'

    if schedule['plan_type'] == 'normal':
        cursor.execute("UPDATE schedules SET leave_until = ? WHERE user_id = ?", (date_str, user_id))
        result = 'success_normal'
    elif schedule['plan_type'] == 'habit':
        if schedule['habit_used_leave_days'] < schedule['habit_total_leave_days']:
            cursor.execute("UPDATE schedules SET leave_until = ?, habit_used_leave_days = habit_used_leave_days + 1 WHERE user_id = ?", (date_str, user_id))
            result = 'success_habit'
        else:
            result = 'no_days_left'
    else:
        result = 'no_plan'

    conn.commit()
    conn.close()
    return result
