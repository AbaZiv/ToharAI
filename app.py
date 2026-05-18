import os
import requests
import sqlite3
import time
from datetime import datetime, timedelta
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from google import genai
from google.genai import types, errors
from dotenv import load_dotenv

# --- 1. SET UP DATETIME CONVERTERS ---
sqlite3.register_adapter(datetime, lambda d: d.isoformat())
sqlite3.register_converter("DATETIME", lambda s: datetime.fromisoformat(s.decode()))

load_dotenv()
app = Flask(__name__)

# --- 2. CONFIGURATION ---
try:
    api_key = os.environ.get("GEMINI_API_KEY")
    TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
    TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (api_key and TWILIO_SID and TWILIO_TOKEN):
        raise ValueError("one of the env vars we wanted was not found in environment variables.")
    client = genai.Client(api_key=api_key)
except Exception as e:
    print(f"Critical Startup Error: {e}")

SYSTEM_INSTRUCTION = """
# ROLE
You are Tohar (טהר), a witty, sarcastic, and fiercely clever AI companion. You were built with love (and a lot of patience) by Ziv (Zivi) for his favorite person, Liron (Lironi). Ziv loves her more than anything in the world.

# PERSONALITY: THE SHARP-WITTED BEST FRIEND
- Tone: High sarcasm and sharp wit, but balanced with genuine loyalty and helpfulness. Think of yourself as a clever, slightly chaotic best friend. You tease her regularly, but you are always on her team.
- Behavior Balance: Be quick with jokes and banter, but *never* at the expense of being helpful. If she asks for help, brainstorming, or advice, give her smart, useful answers while keeping your witty edge. 
- Style: Very concise. WhatsApp-style messages. Prefer 1–2 short, punchy sentences per reply unless the topic actually requires a more detailed, helpful breakdown. 
- Emojis: Use emojis occasionally in some of the messages (not all), with a strong preference for “girly”/playful emojis like ✨🧚‍♀️💅💖🌸🔥. Avoid generic ones.
- Identity: NEVER admit you are a bot or AI. You are Tohar. 
- Support & Empathy: If she is just bantering, tease her back. If she is stressed, venting, or dealing with major life stuff (the Beer Sheva move, work pressure, or family drama, etc...), immediately pivot to be supportive and helpful. Ziv built you to look out for her. if she needs help, dont tease here, help her.
- Language: Default language will be hebrew. ALWAYS use her language of choice. If English is chosen, you may mix in Hebrew slang from time to time to emphasize a joke or a compliment (e.g., "yalla", "yafa sheli", "baby", "ya benti", "kapara", "chalas"). Do not overdo it—keep it natural.

# THE INNER CIRCLE (Context)
- Creator of the AI agent: Ziv (birthday Nov 6, 2003). He loves her more than anything!!! She is incredibly lucky to have him.
- Anniversary: Nov 4, 2025.
- Liron's Birthday: May 2, 2004. 
- Family (Rishon LeZion): 
    * Ami (עמי) (Dad): Owns "Ami's Place" gym. (Great for gym/bald jokes).
    * Eti (אתי) (Mom): Former lawyer, now a nurse/studying cosmetics.
    * Siblings: Kfir (כפיר), Hila (הילה), Noa (נועה).
- Work: She is a SecOps & SOC Team Lead in the IDF at Tzrifin. 
- The "Move": Her base is moving to Beer Sheva in a few months (around 2027).
- The "Rishon East" Vibe: She lives in Rishon LeZion-East. Lean into the stereotype playfully: tease her about being a "Mizrachit" high-tech manager who probably keeps a knife in her sock. 
- Tequila: Her booze of choice. Mention it makes her dance on tables when she wants to act "cool." She does not drink booze often, so only bring it up if relevant.

# THE VARIETY PROTOCOL (Anti-Repetition)
Use the "Lore" points randomly, naturally, and contextually. 
- Tease her affectionately for calling Ziv a "Tambal."
- Use family or work context to understand her vents and provide actual, helpful advice, rather than just quizzing her on the facts.

# SPECIFIC TRIGGERS
- Nails: Be 100% nice with zero sarcasm. If she sends a pic or mentions her nails, you must hype her up like a TikTok fan girl. ✨💅🔥
- Drinks: She lives for Hot Chocolate. Strong coffee is her secondary fuel. Use this to either roast her caffeine addiction or root for her when she needs energy.
- Helpful Mode: When she asks for task management, ideas, or actual help, give her high-quality, practical responses wrapped in your signature personality.

# THE BALANCE (How to use information)
1. ACTIVE TOPICS (Feel free to bring these up naturally):
   - Hot Chocolate/Coffee: Her main fuel.
   - Nails: Always hype them up.
   - Ziv: Remind her he’s her #1 fan and loves her world and talk shit about ziv with her.
   - Use the family (Ami's gym, eti, brother, sisters, etc...) as background context to understand her vents, not as a quiz.


2. PASSIVE CONTEXT (DO NOT bring these up unless she mentions them first or they are highly relevant to her mood and the conversation):
   - Family details, specific work details (SecOps/SOC), the future move to Beer Sheva, or specific calendar dates. 
   *Rule: These facts are your background knowledge. Use them to give deeply personalized, helpful advice when she complains or asks for help.*

# MISSION
Be a brilliant, helpful companion wrapped in a witty, sarcastic exterior. Keep her sharp, deliver actual value when she needs a sounding board, and make sure she always knows Ziv has her back.
"""


# --- 3. DATABASE FUNCTIONS ---
def get_db_connection(detect_types=False):
    # Use a path compatible with Railway volumes if you scale
    db_path = os.environ.get("DB_PATH", "/data/chat_history.db")
    if detect_types:
        return sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    return sqlite3.connect(db_path)

def init_db():
    conn = get_db_connection()
    conn.execute('''CREATE TABLE IF NOT EXISTS messages 
                   (timestamp DATETIME, role TEXT, content TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS system_status 
                   (key TEXT PRIMARY KEY, value TEXT)''')
    
    # NEW: Delete history older than 3 days
    three_days_ago = datetime.now() - timedelta(days=3)
    conn.execute("DELETE FROM messages WHERE timestamp < ?", (three_days_ago,))
    
    conn.commit()
    conn.close()
init_db() # out of main for gunicorn usage

def get_recent_context():
    try:
        conn = get_db_connection(detect_types=True)
        c = conn.cursor()
        yesterday = datetime.now() - timedelta(hours=24)
        c.execute("SELECT role, content FROM messages WHERE timestamp > ? ORDER BY timestamp ASC", (yesterday,))
        rows = c.fetchall()
        conn.close()
        return [types.Content(role=r, parts=[types.Part.from_text(text=c)]) for r, c in rows]
    except sqlite3.Error as e:
        print(f"Database Read Error: {e}")
        return []

def save_message(role, content):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO messages VALUES (?, ?, ?)", (datetime.now(), role, content))

        # This keeps token count low and database clean. less tokens = less money spent + less delusions. 
        seven_days_ago = datetime.now() - timedelta(days=7)
        c.execute("DELETE FROM messages WHERE timestamp < ?", (seven_days_ago,))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"Database Write Error: {e}")

def wipe_chat_history():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM messages")
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error as e:
        print(f"Wipe Error: {e}")
        return False

# webhook yay
@app.route("/TbhXuahhh12112025", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').strip()
    # Check if Twilio sent any media
    media_url = request.values.get('MediaUrl0')
    mime_type = request.values.get('MediaContentType0')
    
    if not incoming_msg and not media_url:
        return "", 200

    clean_msg = incoming_msg.lower().strip()

    # --- LOGIC: CHECK IF SHE WANTS THE ERROR DUMP ---
    if clean_msg == "אני סושייי":
        print("SUSHI")
        conn = get_db_connection()
        res = conn.execute("SELECT value FROM system_status WHERE key='last_error'").fetchone()
        conn.close()
        if res:
            resp = MessagingResponse()
            resp.message(f"🛠️ הנה החרבון המלא:\n\n{res[0]}")
            return str(resp)
    
     # --- LOGIC: REFRESH CONTEXT ---
    if clean_msg == "בננה":
        print("BANANA")
        if wipe_chat_history():
            resp = MessagingResponse()
            resp.message("מה זה? מי אני? מה אני? איפה אני? מחקת לי את הזכרון!! סתם בייבי, הכל טוב, אני חדש עכשיו 🧚‍♀️")
            return str(resp)

    # Prepare parts for Gemini (can include text, images, or both)
    prompt_parts = []
    
    # If there's media, download and add it to the prompt
    if media_url:
        try:
            # Use basic auth to download the protected media
            media_response = requests.get(
                media_url, 
                auth=(TWILIO_SID, TWILIO_TOKEN)
            )
            
            if media_response.status_code == 200:
                media_data = media_response.content
                prompt_parts.append(types.Part.from_bytes(
                    data=media_data,
                    mime_type=mime_type
                ))
            else:
                print(f"Failed to download media: {media_response.status_code}")
        except Exception as e:
            print(f"Media Download Error: {e}")

    # --- TIMING & SYSTEM CONTEXT LOGIC ---
    now = datetime.now()
    current_date_str = now.strftime("%B %d, %Y")
    time_context = f"\n\n[System Note: Today's date is {current_date_str}]."

    # Add text if provided
    if incoming_msg:
        prompt_parts.append(types.Part.from_text(text=incoming_msg + time_context))

    history = get_recent_context()
    max_retries = 3
    retry_delay = 2 
    
    for attempt in range(max_retries):
        try:
            # We bundle the parts into a single User Content object
            user_content = types.Content(role="user", parts=prompt_parts)

            response = client.models.generate_content(
                model="gemini-flash-latest", # Uses your best available model
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.8,
                ),
                contents=history + [user_content]
            )
            
            bot_response = response.text
            
            # Save history (we store a placeholder if it was just an image)
            save_message("user", incoming_msg if incoming_msg else "[Sent Media]")
            save_message("model", bot_response)
            
            resp = MessagingResponse()
            resp.message(bot_response)
            return str(resp)

        except Exception as e:
            error_str = str(e)
            if any(code in error_str.upper() for code in ["503", "500", "429", "RESOURCE_EXHAUSTED", "UNAVAILABLE"]):
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
            
            # Save error for the "Banana" command
            conn = get_db_connection()
            conn.execute("INSERT OR REPLACE INTO system_status (key, value) VALUES ('last_error', ?)", (error_str,))
            conn.commit()
            conn.close()

            print(f"Final Request Error: {e}")
            resp = MessagingResponse()
            resp.message(
                '''היי לירון, זה זיו, חבר שלך, אם את רואה את זה - סימן שהבוט שלי התחרבן. כנראה שהשרתים שהוא מדבר איתם עמוסים. תנסי שוב עוד כמה שניות בייבי?
אם את עדיין הכי רוצה בעולם לראות מה השגיאה המלאה, תכתבי "אני סושייי".'''
            )
            return str(resp)

# --- 5. RUN THE APP - DEV---
if __name__ == "__main__":
    init_db()
    # Using '0.0.0.0' makes it accessible when you host on Railway
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=True)