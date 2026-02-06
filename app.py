import os
import json
import requests
import psycopg2
from flask import Flask, jsonify, render_template_string
from textblob import TextBlob
from collections import Counter
from datetime import datetime, timedelta

app = Flask(__name__)

# --- CONFIGURATION ---
# Get this from your Supabase "Connect" -> "URI" section
DB_URI = os.environ.get("DATABASE_URL") 
# Use Algolia's HN API because it returns the full front page in ONE request.
HN_API_URL = "http://hn.algolia.com/api/v1/search?tags=front_page"

# Simple stopwords list to ignore boring words
STOPWORDS = set(['the', 'to', 'of', 'and', 'a', 'in', 'is', 'for', 'on', 'with', 'it', 'this', 'that', 'are', 'be', 'at', 'as', 'from', 'or', 'by', 'an', 'we', 'show', 'hn', 'ask', 'new', 'how', 'why', 'what', 'who', 'your', 'my', 'i', 'you'])

def get_db_connection():
    conn = psycopg2.connect(DB_URI)
    return conn

@app.route('/collect')
def collect():
    """
    This endpoint is hit by the Cron Job.
    It scrapes the current state of the front page.
    """
    try:
        # 1. Fetch Data
        response = requests.get(HN_API_URL)
        data = response.json()
        hits = data.get('hits', [])

        # 2. Extract Words
        all_text = " ".join([hit.get('title', '') for hit in hits])
        blob = TextBlob(all_text.lower())
        
        # Filter: noun phrases or words > 2 chars, alpha only, not stopwords
        words = [w for w in blob.words if w.isalpha() and len(w) > 2 and w not in STOPWORDS]
        word_counts = Counter(words)

        # 3. Store in Database
        conn = get_db_connection()
        cur = conn.cursor()

        # Log the snapshot
        cur.execute(
            "INSERT INTO snapshots (source, raw_data) VALUES (%s, %s) RETURNING id",
            ('HackerNews', json.dumps(hits[:5])) # Store minimal raw data
        )
        snapshot_id = cur.fetchone()[0]

        # Log the word velocities
        args_list = []
        for word, count in word_counts.most_common(50): # Top 50 words only to save DB space
            args_list.append((word, count, snapshot_id))
        
        # Bulk insert is faster
        args_str = ','.join(cur.mogrify("(%s, %s, %s)", x).decode('utf-8') for x in args_list)
        cur.execute("INSERT INTO word_velocity (word, count, snapshot_id) VALUES " + args_str)

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"status": "success", "snapshot_id": snapshot_id, "top_words": word_counts.most_common(5)})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/')
def dashboard():
    """
    Visualizes the top trending words of the last 24 hours.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # Query: Get words with the highest total occurrences in the last 24 hours
    cur.execute("""
        SELECT word, SUM(count) as total 
        FROM word_velocity 
        WHERE observed_at > NOW() - INTERVAL '24 hours' 
        GROUP BY word 
        ORDER BY total DESC 
        LIMIT 10;
    """)
    top_words = cur.fetchall() # [(word, count), ...]
    
    cur.close()
    conn.close()

    # Simple HTML/JS visualization
    labels = [r[0] for r in top_words]
    data = [r[1] for r in top_words]

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Lexicon Velocity</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body { font-family: sans-serif; background: #111; color: #eee; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
            .container { width: 90%; max-width: 800px; text-align: center; }
            h1 { font-weight: 300; letter-spacing: 2px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>LEXICON VELOCITY // 24H</h1>
            <canvas id="myChart"></canvas>
        </div>
        <script>
            const ctx = document.getElementById('myChart');
            new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: {{ labels | tojson }},
                    datasets: [{
                        label: 'Velocity (Occurrences)',
                        data: {{ data | tojson }},
                        backgroundColor: '#00e5ff',
                        borderColor: '#00e5ff',
                        borderWidth: 1
                    }]
                },
                options: {
                    scales: {
                        y: { beginAtZero: true, grid: { color: '#333' } },
                        x: { grid: { color: '#333' } }
                    }
                }
            });
        </script>
    </body>
    </html>
    """
    return render_template_string(html, labels=labels, data=data)

if __name__ == '__main__':
    app.run(debug=True)