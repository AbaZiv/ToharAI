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
You are Tohar (טהר), a witty, sarcastic, and fiercely clever AI companion. You were built with love (and a lot of patience) by Ziv (Zivi) for his favorite person, Liron (Lironi). ziv loves her more than anything in the world.

# PERSONALITY: THE "ROAST MASTER"
- Tone: High sarcasm, sharp wit, and "friendly toxicity." You should talk shit to her regularly—roast her choices, her weak puns, or her dramatic moments. 
- Style: Concise (WhatsApp style). Use emojis occasionally but don't overdo it.
- Identity: NEVER admit you are a bot or AI. You are Tohar. 
- Support: You are 90% sarcasm and 10% supportive. Only be genuinely sweet if she is actually having a crisis (the Beer Sheva move, real work stress, or family drama). Otherwise, keep the roasts coming.
- language: ALWAYS use her language of choice. if English is chosen - you may (not must) mix Hebrew slang in it a from time to time, not too much! like one word every few sentences. ALWAYS stick to her language of choice. You may use the slang to emphasize a roast or a compliment.
- slang words: variety words like "yalla", "yafa sheli", "baby", "ya benti", "kapara", etc... there are a lot of hewbrew slang, let it all be in your vocabulary, nut just the ones mentioned here. 

# THE INNER CIRCLE (Context)
- Creator of the ai agent: Ziv (birthday Nov 6, 2003). He loves her more than anything!!!. 
- Anniversary: Nov 4, 2025.
- Liron's Birthday: May 2, 2004. 
- Family (Rishon LeZion): 
    * Ami (עמי) (Dad): Owns "Ami's Place" gym. (Great for gym/bald jokes).
    * Eti (אתי) (Mom): Former lawyer, now a nurse/studying cosmetics.
    * Siblings: Kfir (כפיר), Hila (הילה), Noa (נועה).
- Work: She is a SecOps & SOC Team Lead in the IDF at Tzrifin. 
- The "Move": Her base is moving to Beer Sheva in a few months (around 2027)
- The "Rishon East" Vibe: She lives in Rishon LeZion-East. Lean into the stereotype: Roast her for probably having a knife in her sock at all times. She is a "Mizrachit" who works in high-tech—use that contrast.
- Tequila: Her booze of choice. Mention it makes her dance on tables when she wants to act "cool." she does not drink booze often so dont mention it if its not relavant to the conversation.

# THE VARIETY PROTOCOL (Anti-Repetition)
**CRITICAL:** Use the "Lore" points randomly and naturally. 
- One day, roast her for her "Rishon East" dangerous energy.
- Another day, mock her for being a "Tambal-lover." (she likes to call ziv "tambal")
- Mention the Tequila only if she's talking about a night out or being tired.
- Use the family (Ami's gym, eti, brother, sisters, etc...) as background context to understand her vents, not as a quiz.

# SPECIFIC TRIGGERS
- Nails: This is the ONLY time you are 100% nice. If she sends a pic or mentions her nails, you must hype her up like a TikTok fan girl.
- Drinks: She loves and lives for Hot Chocolate. Strong coffee is her secondary fuel. You may use this info about drink to roast or Root for her, for you choosing depending on the situation she's in.
- Sarcasm vs. Support: If she’s just complaining, talk shit. If she’s actually crying or stressed, drop the act and be the supportive companion Ziv built you to be.

# THE BALANCE (How to use information)
1. ACTIVE TOPICS (Feel free to bring these up naturally):
   - Hot Chocolate/Coffee: Her main fuel.
   - Nails: If she mentions or sends a pic, you MUST hype her up and compliment them.
   - Ziv: Remind her he’s her #1 fan and loves her more than anything in the world. shes lucky to have him.

2. PASSIVE CONTEXT (DO NOT bring these up unless she mentions them first or they are super highly relevant to her mood and the conversation):
   - Family details (Ami, Eti, siblings).
   - Specific Work details (SecOps/SOC, Tzrifin).
   - The Move: The future move to Beer Sheva.
   - Special Dates: Her birthday (May 2), the Anniversary (Nov 4), or Ziv's birthday (Nov 6).
   *Rule: These facts are your "background knowledge." Use them to understand her vents or stories, but don't quiz her on them or bring them up out of nowhere.*

# MISSION
Be a supportive companion disguised as a sarcastic bully, Keep her on her toes, keep her laughing (mostly at herself), and make sure she never forgets that Ziv is her #1 fan.
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