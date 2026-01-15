from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, Blueprint
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
import random
import json
import os
import copy
import mercadopago
from datetime import datetime, timedelta
import hashlib
import hmac
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import requests
from dotenv import load_dotenv

# Cargar variables de entorno
if os.path.exists('.env'):
    load_dotenv()

# ==============================
# APP
# ==============================
app = Flask(__name__)
basedir = os.path.abspath(os.path.dirname(__file__))

# ==============================
# CONFIGURACIÓN
# ==============================
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-key-change-in-production')

# DEBUG: Ver si la SECRET_KEY se cargó
print(f"🔍 SECRET_KEY detectada: {app.config['SECRET_KEY'][:20]}..." if app.config['SECRET_KEY'] else "❌ SECRET_KEY NO DETECTADA")
print(f"🔍 Variables de entorno disponibles: {list(os.environ.keys())[:10]}")

# DEBUG: Ver qué base de datos se va a usar
database_url = os.getenv('DATABASE_URL', 'sqlite:///instance/users.db')

# Fix: PostgreSQL URL debe usar postgresql+psycopg en vez de postgresql
if database_url and database_url.startswith('postgresql://'):
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg://')
    print(f"🔍 DATABASE_URL convertida a: {database_url[:50]}...")
else:
    print(f"🔍 DATABASE_URL encontrada: {database_url[:50] if database_url else 'NO ENCONTRADA'}")

# Usar PostgreSQL si DATABASE_URL existe, sino SQLite local
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# ==============================
# SPOTIFY CONFIGURATION CON OAUTH
# ==============================
import spotipy
from spotipy.oauth2 import SpotifyOAuth

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "https://www.progressivejourney.net/spotify/callback")

# ==============================
# YOUTUBE API CONFIGURATION
# ==============================
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")  # Agregar tu API key aquí

# Cache de tokens por usuario
spotify_tokens = {}

def get_spotify_client(user_id):
    """Obtiene un cliente de Spotify autenticado para el usuario."""
    cache_path = f".spotify_cache_{user_id}"
    
    auth_manager = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="user-read-private",
        cache_path=cache_path,
        show_dialog=False
    )
    
    return spotipy.Spotify(auth_manager=auth_manager)

# ==============================
# SPOTIFY CLIENT CREDENTIALS (SIN OAUTH)
# ==============================
def get_spotify_token_public():
    """Obtiene token público de Spotify (Client Credentials)."""
    try:
        auth_response = requests.post('https://accounts.spotify.com/api/token', {
            'grant_type': 'client_credentials',
            'client_id': SPOTIFY_CLIENT_ID,
            'client_secret': SPOTIFY_CLIENT_SECRET,
        })
        
        if auth_response.status_code == 200:
            return auth_response.json()['access_token']
        return None
    except Exception as e:
        print(f"Error getting Spotify token: {e}")
        return None

def search_spotify_id(artist, track):
    """Busca el Spotify ID de un track (sin OAuth)."""
    try:
        token = get_spotify_token_public()
        if not token:
            return None
        
        headers = {'Authorization': f'Bearer {token}'}
        search_url = 'https://api.spotify.com/v1/search'
        
        params = {
            'q': f'artist:{artist} track:{track}',
            'type': 'track',
            'limit': 1
        }
        
        response = requests.get(search_url, headers=headers, params=params)
        
        if response.status_code == 200:
            results = response.json()
            if results['tracks']['items']:
                return results['tracks']['items'][0]['id']
        
        return None
        
    except Exception as e:
        print(f"Error searching Spotify ID: {e}")
        return None

# ==============================
# YOUTUBE SEARCH
# ==============================
def search_youtube_id(artist, track):
    """Busca el YouTube ID de un track."""
    if not YOUTUBE_API_KEY:
        print("⚠️ YouTube API Key no configurada")
        return None
    
    print(f"✅ YouTube API Key encontrada: {YOUTUBE_API_KEY[:20]}...")  # ← AGREGAR
    
    try:
        from googleapiclient.discovery import build
        
        print("✅ googleapiclient importado correctamente")  # ← AGREGAR
        
        youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
        
        print("✅ Cliente de YouTube creado")  # ← AGREGAR
        
        search_query = f"{artist} {track} progressive house"
        
        print(f"🔍 Buscando en YouTube: {search_query}")
        
        request = youtube.search().list(
            part="snippet",
            q=search_query,
            type="video",
            maxResults=1,
            videoCategoryId="10"
        )
        
        print("✅ Request creado, ejecutando...")  # ← AGREGAR
        
        response = request.execute()
        
        print(f"✅ Response recibida: {response}")  # ← AGREGAR
        
        if response['items']:
            video_id = response['items'][0]['id']['videoId']
            print(f"✅ YouTube ID encontrado: {video_id}")
            return video_id
        else:
            print(f"⚠️ No se encontró en YouTube")
        
        return None
        
    except Exception as e:
        print(f"❌ Error searching YouTube: {e}")
        import traceback
        traceback.print_exc()  # ← AGREGAR (muestra error completo)
        return None

# ==============================
# DATABASE
# ==============================
db = SQLAlchemy(app)

# ==============================
# LOGIN
# ==============================
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# ==============================
# MODELOS (ANTES DE db.create_all())
# ==============================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='trial') 
    trial_uses_left = db.Column(db.Integer, default=2)
    pro_until = db.Column(db.DateTime, nullable=True)
    plan = db.Column(db.String(10))
    last_payment_id = db.Column(db.String(100))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class SharedSet(db.Model):
    id = db.Column(db.String(10), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    setlist_json = db.Column(db.Text, nullable=False)
    duration_hours = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    views = db.Column(db.Integer, default=0)
    
    def __repr__(self):
        return f'<SharedSet {self.id}>'

class PaymentRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    plan = db.Column(db.String(20), nullable=False)  # 'monthly' o 'annual'
    amount_usdt = db.Column(db.Float, nullable=False)
    tx_id = db.Column(db.String(200), nullable=True)
    screenshot_data = db.Column(db.Text, nullable=True)  # Base64 de la imagen
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)
    
    user = db.relationship('User', backref='payment_requests')
    
    def __repr__(self):
        return f'<PaymentRequest {self.id} - {self.user.email} - {self.plan}>'

# ==============================
# INICIALIZAR DB (DESPUÉS DE DEFINIR MODELOS)
# ==============================
with app.app_context():
    db.create_all()
    print("✅ Base de datos inicializada correctamente")
    print("📍 Ubicación:", db.engine.url)
    
    # 👇 AGREGAR ESTO ACÁ
    owner_email = os.getenv('OWNER_EMAIL', 'elidj269@gmail.com')
    owner_password = os.getenv('OWNER_PASSWORD', 'password_default')
    
    existing_owner = User.query.filter_by(email=owner_email).first()
    
    if not existing_owner:
        owner = User(email=owner_email, role='owner')
        owner.set_password(owner_password)
        owner.pro_until = datetime.utcnow() + timedelta(days=3650)
        owner.plan = 'lifetime'
        db.session.add(owner)
        db.session.commit()
        print(f"✅ Usuario owner {owner_email} creado")
    else:
        print(f"✅ Usuario owner ya existe")
        
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def check_pro(user):
    if user.role == 'owner' and user.pro_until:
        if datetime.utcnow() > user.pro_until:
            user.role = 'trial'
            db.session.commit()
            return False
    return user.role == 'owner'

# ==============================
# MERCADOPAGO
# ==============================
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
if not MP_ACCESS_TOKEN:
    print("⚠️ ADVERTENCIA: MP_ACCESS_TOKEN no configurado. Los pagos no funcionarán.")
else:
    sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

CATTANEO_STATE = {
    "rep_count": 0, 
    "last_phase": "",
    "tracks_since_fifth": 0,
    "fifth_count": 0,
    "last_two_keys": [],
    "switch_pair": None,
    "switch_pair_count": 0
}

CATEGORY_MAP = {
    "warm-up": "warmup",
    "building": "build",
    "mid-peak": ["mid_peak", "midpeaks"],
    "peak time": "peaktime",
    "driving": "driving",
    "closing": "closing"
}

ENERGY_RANGES_PRO = {
    "warmup": {
        "bpm": (115, 120), 
        "keys": ["1A","2A","3A","4A","5A","6A","7A","8A","12A"],
        "energy": (1, 4)
    },
    "build": {
        "bpm": (118, 122), 
        "keys": ["5A","6A","7A","8A","9A","10A","11A","12A","7B","8B","9B"],
        "energy": (4, 6)
    },
    "mid_peak": {
        "bpm": (121, 123), 
        "keys": ["8A","9A","10A","11A","12A","1A","2A","8B","9B","10B","11B","12B","1B"],
        "energy": (6, 8)
    },
    "peak_time": {
        "bpm": (123, 125), 
        "keys": ["11A","12A","1A","2A","3A","11B","12B","1B","2B","3B"],
        "energy": (7, 9)
    },
    "driving": {
        "bpm": (124, 126), 
        "keys": ["12B","1B","2B","3B","4B","12A","1A","2A","3A"],
        "energy": (8, 10)
    },
    "closing": {
        "bpm": (120, 124), 
        "keys": ["10B", "11B", "12B", "1B", "2B", "3B", "7A", "8B", "9B"],
        "energy": (4, 7),
        "bpm_step_max": 4
    }
}

MUSICAL_TO_CAMELOT = {
    "C Minor": "5A", "G Minor": "6A", "D Minor": "7A", "A Minor": "8A",
    "E Minor": "9A", "B Minor": "10A", "F# Minor": "11A", "Gb Minor": "11A",
    "C# Minor": "12A", "Db Minor": "12A", "Ab Minor": "1A", "G# Minor": "1A",
    "Eb Minor": "2A", "D# Minor": "2A", "Bb Minor": "3A", "A# Minor": "3A",
    "F Minor": "4A", "C Major": "8B", "G Major": "9B", "D Major": "10B", 
    "A Major": "11B", "E Major": "12B", "B Major": "1B", "F# Major": "2B", 
    "Gb Major": "2B", "Db Major": "3B", "C# Major": "3B", "Ab Major": "4B", 
    "G# Major": "4B", "Eb Major": "5B", "D# Major": "5B", "Bb Major": "6B", 
    "A# Major": "6B", "F Major": "7B"
}

PHASE_VARIANTS = {
    1: [
        ["warmup"]*2 + ["build"]*3 + ["mid_peak"]*5 + ["peak_time"]*1 + ["closing"]*1,
        ["warmup"]*3 + ["build"]*3 + ["mid_peak"]*5 + ["peak_time"]*1 + ["closing"]*1,
        ["warmup"]*2 + ["build"]*4 + ["mid_peak"]*4 + ["peak_time"]*1 + ["closing"]*1,
        ["warmup"]*3 + ["build"]*4 + ["mid_peak"]*4 + ["peak_time"]*1 + ["closing"]*1,
    ],
    2: [
        ["warmup"]*4 + ["build"]*5 + ["mid_peak"]*7 + ["peak_time"]*3 + ["driving"]*2 + ["closing"]*1,
        ["warmup"]*5 + ["build"]*5 + ["mid_peak"]*6 + ["peak_time"]*3 + ["driving"]*2 + ["closing"]*1,
        ["warmup"]*4 + ["build"]*6 + ["mid_peak"]*6 + ["peak_time"]*3 + ["driving"]*2 + ["closing"]*1,
        ["warmup"]*5 + ["build"]*4 + ["mid_peak"]*7 + ["peak_time"]*3 + ["driving"]*2 + ["closing"]*1,
    ],
    3: [
        ["warmup"]*6 + ["build"]*7 + ["mid_peak"]*8 + ["peak_time"]*4 + ["driving"]*2 + ["closing"]*1,
        ["warmup"]*7 + ["build"]*7 + ["mid_peak"]*7 + ["peak_time"]*4 + ["driving"]*2 + ["closing"]*1,
        ["warmup"]*6 + ["build"]*8 + ["mid_peak"]*7 + ["peak_time"]*4 + ["driving"]*2 + ["closing"]*1,
        ["warmup"]*6 + ["build"]*6 + ["mid_peak"]*9 + ["peak_time"]*4 + ["driving"]*2 + ["closing"]*1,
    ],
    4: [
        ["warmup"]*8 + ["build"]*9 + ["mid_peak"]*9 + ["peak_time"]*6 + ["driving"]*3 + ["closing"]*2,
        ["warmup"]*7 + ["build"]*7 + ["mid_peak"]*9 + ["peak_time"]*6 + ["driving"]*3 + ["closing"]*2,
        ["warmup"]*6 + ["build"]*9 + ["mid_peak"]*10 + ["peak_time"]*5 + ["driving"]*3 + ["closing"]*2,
        ["warmup"]*8 + ["build"]*8 + ["mid_peak"]*10 + ["peak_time"]*5 + ["driving"]*3 + ["closing"]*2,
    ],
    5: [
        ["warmup"]*9 + ["build"]*10 + ["mid_peak"]*11 + ["peak_time"]*6 + ["driving"]*5 + ["closing"]*3,
        ["warmup"]*8 + ["build"]*11 + ["mid_peak"]*11 + ["peak_time"]*6 + ["driving"]*5 + ["closing"]*3,
        ["warmup"]*8 + ["build"]*10 + ["mid_peak"]*12 + ["peak_time"]*6 + ["driving"]*5 + ["closing"]*3,
        ["warmup"]*10 + ["build"]*10 + ["mid_peak"]*10 + ["peak_time"]*6 + ["driving"]*5 + ["closing"]*3,
    ]
}

def normalize_key(key_str):
    if not key_str: return key_str
    key_str = key_str.strip()
    if len(key_str) >= 2 and key_str[:-1].isdigit() and key_str[-1] in ["A","B"]: return key_str
    return MUSICAL_TO_CAMELOT.get(key_str, key_str)

tracks_cache = []
def load_tracks():
    global tracks_cache
    if tracks_cache: return tracks_cache
    path = os.path.join("data", "tracks.json")
    if not os.path.exists(path): return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        tracks_cache = data if isinstance(data, list) else data.get("tracks", [])
    for t in tracks_cache: t["key"] = normalize_key(t.get("key"))
    return tracks_cache

def save_tracks(tracks):
    """Guarda los tracks actualizados en el JSON."""
    path = os.path.join("data", "tracks.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tracks, f, ensure_ascii=False, indent=2)
    
    # Actualizar cache
    global tracks_cache
    tracks_cache = tracks

def is_track_valid_for_phase(track, phase, attempt=1):
    config = ENERGY_RANGES_PRO.get(phase)
    if not config: return True
    try:
        bpm = float(track.get("bpm", 0))
        key_val = track.get("key", "")
        energy = int(track.get("energy", 5))
    except: 
        return False
    
    min_bpm, max_bpm = config["bpm"]
    min_energy, max_energy = config["energy"]
    
    margin = 2 if attempt > 1 else 0
    energy_margin = 1 if attempt > 1 else 0
    
    bpm_ok = (min_bpm - margin) <= bpm <= (max_bpm + margin)
    key_ok = key_val in config["keys"]
    energy_ok = (min_energy - energy_margin) <= energy <= (max_energy + energy_margin)
    
    return bpm_ok and key_ok and energy_ok

def camelot_relation(prev_key, curr_key):
    try:
        n1, l1 = int(prev_key[:-1]), prev_key[-1]
        n2, l2 = int(curr_key[:-1]), curr_key[-1]
        if n1 == n2 and l1 == l2: return "same"
        if n1 == n2 and l1 != l2: return "switch"
        if l1 == l2:
            if (n1 % 12) + 1 == n2: return "up"
            if (n1 - 2) % 12 + 1 == n2: return "down"
            if (n1 + 4) % 12 + 1 == n2 or (n1 - 6) % 12 + 1 == n2: return "fifth"
        return "invalid"
    except: return "invalid"

def get_max_fifths_allowed(duration_hours):
    if duration_hours <= 1:
        return 1
    elif duration_hours == 2:
        return 2
    else:
        return min(4, 3 + (duration_hours - 3))

def check_repetition_pattern(current_key, last_two_keys):
    if len(last_two_keys) < 2:
        return True
    if last_two_keys[0] == last_two_keys[1] == current_key:
        return False
    return True

def get_key_pair(key1, key2):
    num1, mode1 = int(key1[:-1]), key1[-1]
    num2, mode2 = int(key2[:-1]), key2[-1]
    if num1 == num2 and mode1 != mode2:
        return f"{num1}A-{num1}B"
    return None

def find_compatible_track(prev_track, target_energy, used_tracks_names, duration_hours=1, recent_keys=None):
    """Encuentra el mejor track compatible con VARIEDAD FORZADA."""
    global CATTANEO_STATE
    all_tracks = load_tracks()
    
    if recent_keys is None:
        recent_keys = []
    
    # Filtrar candidatos válidos
    candidates = [t for t in all_tracks if t["track"] not in used_tracks_names and is_track_valid_for_phase(t, target_energy)]
    if not candidates:
        candidates = [t for t in all_tracks if t["track"] not in used_tracks_names and is_track_valid_for_phase(t, target_energy, attempt=2)]
    if not candidates:
        return None
    
    # Si no hay track previo, elegir uno al azar
    if not prev_track:
        chosen = random.choice(candidates).copy()
        chosen["stage"] = target_energy
        return chosen
    
    prev_key = prev_track.get("key", "7A")
    prev_mode = prev_key[-1]
    
    max_fifths = get_max_fifths_allowed(duration_hours)

    if CATTANEO_STATE["last_phase"] != target_energy:
        CATTANEO_STATE["rep_count"] = 0
        CATTANEO_STATE["last_phase"] = target_energy

    # 🔥 PENALIZACIÓN POR KEYS RECIENTES (últimas 5)
    recent_keys_set = set(recent_keys[-5:]) if len(recent_keys) > 0 else set()
    
    scored = []
    
    for t in candidates:
        rel = camelot_relation(prev_key, t["key"])
        if rel == "invalid":
            continue
        
        current_key = t["key"]
        
        if not check_repetition_pattern(current_key, CATTANEO_STATE["last_two_keys"]):
            continue
        
        current_pair = get_key_pair(prev_key, current_key)
        if rel == "switch" and current_pair:
            if CATTANEO_STATE["switch_pair"] == current_pair:
                if CATTANEO_STATE["switch_pair_count"] >= 2:
                    continue
        
        score = random.uniform(20, 40)

        # 🔥 PENALIZACIÓN FUERTE SI LA KEY YA SE USÓ RECIENTEMENTE
        if current_key in recent_keys_set:
            score -= 500
        
        # 🔥 PENALIZACIÓN ADICIONAL SI ES LA MISMA KEY QUE LA ANTERIOR
        if current_key == prev_key:
            score -= 200

        fifth_penalty = 0
        if rel == "fifth":
            if CATTANEO_STATE["fifth_count"] >= max_fifths:
                continue
            if CATTANEO_STATE["tracks_since_fifth"] < 10:
                fifth_penalty = -300
            else:
                fifth_penalty = 100

        if rel == "same":
            if CATTANEO_STATE["rep_count"] < 1:
                score += 40
            elif CATTANEO_STATE["rep_count"] == 1:
                score += 20
            else:
                score -= 300
        elif rel == "up":
            base_up_score = 200
            if CATTANEO_STATE["switch_pair_count"] >= 2:
                base_up_score += 100
            score += base_up_score
        elif rel == "down":
            score += 120
        elif rel == "switch":
            switch_score = 180
            if current_pair and CATTANEO_STATE["switch_pair"] == current_pair:
                if CATTANEO_STATE["switch_pair_count"] >= 1:
                    switch_score -= 80
            score += switch_score
        elif rel == "fifth":
            score += fifth_penalty

        if target_energy in ["build", "mid_peak"] and prev_mode == "A" and current_key.endswith("B"):
            score += 150

        if target_energy in ["warmup", "build"] and current_key.endswith("A"):
            score += 80

        scored.append((score, t))

    if not scored:
        return None
    
    scored.sort(key=lambda x: x[0], reverse=True)
    
    # 🔥 SELECCIÓN CON VARIEDAD: Top 10% con algo de randomness
    top_candidates = scored[:max(1, len(scored) // 10)]
    ganador = random.choice(top_candidates)[1].copy()
    ganador_key = ganador["key"]
    ganador_rel = camelot_relation(prev_key, ganador_key)

    CATTANEO_STATE["last_two_keys"].append(ganador_key)
    if len(CATTANEO_STATE["last_two_keys"]) > 2:
        CATTANEO_STATE["last_two_keys"].pop(0)
    
    ganador_pair = get_key_pair(prev_key, ganador_key)
    if ganador_rel == "switch" and ganador_pair:
        if CATTANEO_STATE["switch_pair"] == ganador_pair:
            CATTANEO_STATE["switch_pair_count"] += 1
        else:
            CATTANEO_STATE["switch_pair"] = ganador_pair
            CATTANEO_STATE["switch_pair_count"] = 1
    else:
        CATTANEO_STATE["switch_pair"] = None
        CATTANEO_STATE["switch_pair_count"] = 0
    
    if ganador_rel == "fifth":
        CATTANEO_STATE["tracks_since_fifth"] = 0
        CATTANEO_STATE["fifth_count"] += 1
    else:
        CATTANEO_STATE["tracks_since_fifth"] += 1

    if ganador_key == prev_key:
        CATTANEO_STATE["rep_count"] += 1
    else:
        CATTANEO_STATE["rep_count"] = 0

    ganador["stage"] = target_energy
    return ganador

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
        else:
            flash("Email o contraseña inválidos")
    return redirect(url_for("index", login_attempt=True)) 

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        if User.query.filter_by(email=email).first():
            flash("Ese email ya está registrado.")
            return redirect(url_for("index", signup_attempt=True)) 
        new_user = User(email=email)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        flash("¡Registro exitoso! Ya puedes iniciar sesión.")
    return redirect(url_for("index", signup_attempt=True)) 

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))

@app.route("/")
def index():
    show_signup = request.args.get("signup_attempt", False)
    return render_template("index.html", user=current_user, show_signup=show_signup) 

@app.route("/api/search")
@login_required 
def api_search():
    tracks = load_tracks()
    q = request.args.get("q", "").lower()
    energy_web = request.args.get("energy", "").lower() 
    page = int(request.args.get("page", 1))
    per_page = 100 
    results = []
    
    web_to_internal = {
        "warm-up": "warmup",
        "building": "build",
        "mid-peak": "mid_peak",
        "peak time": "peaktime",
        "driving": "driving",
        "closing": "closing"
    }
    
    target_stage = web_to_internal.get(energy_web)

    for t in tracks:
        full = f"{t.get('artist','')} {t.get('track','')}".lower()
        if q and q not in full: continue
        
        current_stage = t.get("stage", "").lower()

        if target_stage:
            if target_stage == "mid_peak":
                if current_stage not in ["mid_peak", "midpeaks"]:
                    continue
            else:
                if current_stage != target_stage:
                    continue
            
        results.append(t)
        
    start = (page - 1) * per_page
    end = start + per_page
    paginated_results = results[start:end]
    return jsonify({"tracks": paginated_results, "has_more": end < len(results)})

@app.route("/generate", methods=["POST"])
@login_required 
def generate():
    if current_user.role == 'trial':
        data = request.json or {}
        hours = int(data.get("hours", 1))
        if hours > 1: 
            return jsonify({"error": "Los usuarios de prueba solo pueden generar sets de 1 hora."}), 403
        if current_user.trial_uses_left <= 0: 
            return jsonify({"error": "Has agotado tus 2 pruebas gratuitas."}), 403
        current_user.trial_uses_left -= 1
        db.session.commit()
    
    global CATTANEO_STATE
    CATTANEO_STATE = {
        "rep_count": 0, 
        "last_phase": "",
        "tracks_since_fifth": 0,
        "fifth_count": 0,
        "last_two_keys": [],
        "switch_pair": None,
        "switch_pair_count": 0
    }
    
    data = request.json or {}
    hours = int(data.get("hours", 1))
    start_name = data.get("start_track", "").lower()
    tracks = load_tracks()
    
    available_phases = PHASE_VARIANTS.get(hours, PHASE_VARIANTS[1])
    selected_phase = random.choice(available_phases)
    target_length = len(selected_phase)
    
    first = None
    target_energy_first = selected_phase[0]

    if start_name:
        for t in tracks:
            full = f"{t.get('artist','')} - {t.get('track','')}".lower()
            if start_name in full:
                first = t.copy()
                break
    
    if not first:
        warmups = [t for t in tracks if is_track_valid_for_phase(t, target_energy_first)]
        first = random.choice(warmups if warmups else tracks).copy()
    
    first["stage"] = target_energy_first
    
    setlist = [first]
    used_tracks = {first["track"]}
    recent_keys = [first["key"]]
    
    for i in range(1, target_length):
        prev = setlist[-1]
        target_energy = selected_phase[i]
        chosen = find_compatible_track(prev, target_energy, used_tracks, duration_hours=hours, recent_keys=recent_keys)
        if chosen:
            chosen["stage"] = target_energy
            setlist.append(chosen)
            used_tracks.add(chosen["track"])
            recent_keys.append(chosen["key"])
        else:
            fallback_tracks = [t for t in tracks if t["track"] not in used_tracks and is_track_valid_for_phase(t, target_energy, attempt=2)]
            if fallback_tracks:
                fallback = random.choice(fallback_tracks).copy()
                fallback["stage"] = target_energy
                setlist.append(fallback)
                used_tracks.add(fallback["track"])
                recent_keys.append(fallback["key"])
    
    final_setlist = setlist[:target_length]
    
    return jsonify(final_setlist)

@app.route("/api/change_track/<int:index>", methods=["POST"])
@login_required 
def change_track(index):
    data = request.json or {}
    setlist_in = data.get("current_setlist", []) 
    if index < 0 or index >= len(setlist_in): return jsonify({"error": "Index out of range"}), 400
    
    prev_track = setlist_in[index - 1] if index > 0 else None
    used_track_names = {t["track"] for t in setlist_in}
    
    target_stage = setlist_in[index].get("stage", "warmup")
    
    if prev_track:
        replacement_track = find_compatible_track(
            prev_track, target_stage, used_track_names
        )
        if replacement_track: 
            replacement_track["stage"] = target_stage
            return jsonify(replacement_track)
        else: return jsonify({"error": "No compatible alternative found"}), 404
    return jsonify({"error": "Cannot change the first track via this endpoint"}), 400

@app.route("/api/generate_locked", methods=["POST"])
@login_required 
def generate_locked():
    if current_user.role == 'trial':
        hours = int(request.json.get("hours", 1))
        if hours > 1: return jsonify({"error": "Los usuarios de prueba solo pueden generar sets de 1 hora."}), 403
        if current_user.trial_uses_left <= 0: return jsonify({"error": "Has agotado tus pruebas."}), 403
        current_user.trial_uses_left -= 1
        db.session.commit()
    
    data = request.json or {}
    hours = int(data.get("hours", 1))
    locked_setlist = data.get("locked_setlist", [])
    fixed_setlist = [t for t in locked_setlist if t.get("isLocked")]
    phases = random.choice(PHASE_VARIANTS.get(hours, PHASE_VARIANTS[1]))
    
    if len(fixed_setlist) > len(phases): fixed_setlist = fixed_setlist[:len(phases)]
    setlist = copy.deepcopy(fixed_setlist)
    used = {t["track"] for t in setlist}
    
    for i in range(len(setlist), len(phases)):
        if not setlist: break
        prev = setlist[-1]
        target_energy = phases[i]
        chosen = find_compatible_track(prev, target_energy, used, duration_hours=hours)
        if chosen:
            if "isLocked" in chosen: del chosen["isLocked"]
            chosen["stage"] = target_energy
            setlist.append(chosen)
            used.add(chosen["track"])
        else: break
    return jsonify(setlist)

# ==============================
# 🎵 NUEVO: ENDPOINT PARA OBTENER PREVIEW (SPOTIFY + YOUTUBE)
# ==============================
@app.route("/api/get_preview", methods=["POST"])
@login_required
def get_preview():
    """
    Busca preview de un track:
    1. Intenta buscar Spotify ID
    2. Si no encuentra, busca YouTube ID
    3. Guarda el resultado en el JSON
    4. Retorna el ID + tipo
    """
    data = request.json or {}
    artist = data.get("artist", "")
    track_name = data.get("track", "")
    
    if not artist or not track_name:
        return jsonify({"error": "Missing artist or track"}), 400
    
    tracks = load_tracks()
    track_found = None
    track_index = -1
    
    # Buscar el track en el JSON
    for i, t in enumerate(tracks):
        if t.get("artist") == artist and t.get("track") == track_name:
            track_found = t
            track_index = i
            break
    
    if not track_found:
        return jsonify({"error": "Track not found in database"}), 404
    
    # Si ya tiene spotify_id, retornarlo
    if track_found.get("spotify_id"):
        return jsonify({
            "type": "spotify",
            "id": track_found["spotify_id"]
        }), 200
    
    # Si ya tiene youtube_id, retornarlo
    if track_found.get("youtube_id"):
        return jsonify({
            "type": "youtube",
            "id": track_found["youtube_id"]
        }), 200
    
    # Intentar buscar en Spotify primero
    spotify_id = search_spotify_id(artist, track_name)
    
    if spotify_id:
        # Guardar en el JSON
        tracks[track_index]["spotify_id"] = spotify_id
        save_tracks(tracks)
        
        return jsonify({
            "type": "spotify",
            "id": spotify_id
        }), 200
    
    # Si no encontró en Spotify, buscar en YouTube
    youtube_id = search_youtube_id(artist, track_name)
    
    if youtube_id:
        # Guardar en el JSON
        tracks[track_index]["youtube_id"] = youtube_id
        save_tracks(tracks)
        
        return jsonify({
            "type": "youtube",
            "id": youtube_id
        }), 200
    
    # No se encontró preview
    return jsonify({"error": "No preview found"}), 404

@app.route("/api/mercadopago-webhook", methods=["POST"])
def mercadopago_webhook():
    try:
        data = request.get_json()
        
        x_signature = request.headers.get('x-signature')
        x_request_id = request.headers.get('x-request-id')
        
        if not x_signature or not x_request_id:
            app.logger.warning("Webhook sin headers de MercadoPago")
            return jsonify({"status": "invalid headers"}), 400
        
        topic = data.get("topic") or data.get("type")
        
        if topic == "payment":
            payment_id = data.get("data", {}).get("id") or data.get("id")
            
            if not payment_id:
                return jsonify({"status": "no payment_id"}), 400
            
            payment_info = sdk.payment().get(payment_id)
            payment_status = payment_info["response"]["status"]
            
            if payment_status == "approved":
                user_id = payment_info["response"].get("external_reference")
                
                if not user_id:
                    app.logger.error(f"Payment {payment_id} sin external_reference")
                    return jsonify({"status": "no user reference"}), 400
                
                user = User.query.get(int(user_id))
                
                if not user:
                    app.logger.error(f"Usuario {user_id} no encontrado")
                    return jsonify({"status": "user not found"}), 404
                
                if user.last_payment_id == str(payment_id):
                    app.logger.info(f"Pago duplicado ignorado: {payment_id}")
                    return jsonify({"status": "already processed"}), 200
                
                items = payment_info["response"].get("additional_info", {}).get("items", [])
                title = items[0].get("title", "") if items else ""
                
                if "Mensual" in title:
                    user.pro_until = datetime.utcnow() + timedelta(days=30)
                    user.plan = 'monthly'
                elif "Anual" in title:
                    user.pro_until = datetime.utcnow() + timedelta(days=365)
                    user.plan = 'annual'
                
                user.role = 'owner'
                user.last_payment_id = str(payment_id)
                
                db.session.commit()
                
                app.logger.info(f"✅ Pago aprobado para user {user_id}, plan: {user.plan}")
                return jsonify({"status": "processed"}), 200
            
            else:
                app.logger.info(f"Pago {payment_id} en estado: {payment_status}")
                return jsonify({"status": "payment not approved"}), 200
        
        return jsonify({"status": "not a payment notification"}), 200
        
    except Exception as e:
        app.logger.error(f"Error en webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/create-payment", methods=["POST"])
@login_required
def create_payment():
    if check_pro(current_user): 
        return jsonify({"error": "Ya eres usuario PRO activo."}), 400
    
    data = request.get_json() or {}
    subscription_type = data.get("type") 
    
    if subscription_type == 'monthly':
        price, title = 10000.00, "Suscripción PRO Mensual"
    elif subscription_type == 'annual':
        price, title = 50000.00, "Suscripción PRO Anual"
    else: 
        return jsonify({"error": "Tipo de suscripción inválido"}), 400

    preference_data = {
        "items": [
            {
                "title": title,
                "quantity": 1,
                "unit_price": price,
                "currency_id": "ARS"
            }
        ],
        "payer": {
            "email": current_user.email
        },
        "external_reference": str(current_user.id),
        "notification_url": "https://www.progressivejourney.net/api/mercadopago-webhook",
        "auto_return": "approved",
        "back_urls": {
            "success": "https://www.progressivejourney.net/?payment=success",
            "pending": "https://www.progressivejourney.net/?payment=pending",
            "failure": "https://www.progressivejourney.net/?payment=failure"
        }
    }

    preference = sdk.preference().create(preference_data)
    return jsonify({"preference_id": preference["response"]["id"]}), 200

@app.route("/api/share_set", methods=["POST"])
@login_required
def share_set():
    """Guarda un set y genera un link único para compartir."""
    data = request.json or {}
    setlist = data.get("setlist", [])
    hours = data.get("hours", 1)
    
    if not setlist or len(setlist) == 0:
        return jsonify({"error": "No hay setlist para compartir"}), 400
    
    # Generar ID único de 6 caracteres
    import string
    while True:
        share_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        if not SharedSet.query.get(share_id):
            break
    
    # Guardar en la DB
    shared_set = SharedSet(
        id=share_id,
        user_id=current_user.id,
        setlist_json=json.dumps(setlist),
        duration_hours=hours
    )
    db.session.add(shared_set)
    db.session.commit()
    
    # Retornar el link
    share_url = f"https://www.progressivejourney.net/set/{share_id}"
    return jsonify({"share_url": share_url, "share_id": share_id}), 200


@app.route("/set/<share_id>")
def view_shared_set(share_id):
    """Página pública para ver un set compartido."""
    shared_set = SharedSet.query.get(share_id)
    
    if not shared_set:
        return render_template("404.html"), 404
    
    # Incrementar views
    shared_set.views += 1
    db.session.commit()
    
    # Parsear el setlist
    setlist = json.loads(shared_set.setlist_json)
    
    return render_template(
        "shared_set.html",
        setlist=setlist,
        duration=shared_set.duration_hours,
        views=shared_set.views,
        created_at=shared_set.created_at,
        share_id=share_id
    )

# ==============================
# ENDPOINTS DE SPOTIFY CON OAUTH
# ==============================

@app.route("/spotify/login")
@login_required
def spotify_login():
    """Redirige al usuario a Spotify para autenticarse."""
    cache_path = f".spotify_cache_{current_user.id}"
    
    auth_manager = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="user-read-private",
        cache_path=cache_path,
        show_dialog=True
    )
    
    auth_url = auth_manager.get_authorize_url()
    return redirect(auth_url)


@app.route("/spotify/callback")
@login_required
def spotify_callback():
    """Callback después de que el usuario autoriza en Spotify."""
    cache_path = f".spotify_cache_{current_user.id}"
    
    auth_manager = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="user-read-private",
        cache_path=cache_path
    )
    
    code = request.args.get('code')
    if code:
        token_info = auth_manager.get_access_token(code)
        if token_info:
            print(f"✅ Usuario {current_user.id} autenticado en Spotify")
            return redirect("/?spotify_connected=true")
    
    return redirect("/?spotify_error=true")


@app.route("/api/spotify/search", methods=["GET"])
@login_required
def spotify_search():
    """Busca canciones en Spotify con OAuth (sin 403)."""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"tracks": []}), 200
    
    try:
        # Obtener cliente autenticado del usuario
        sp = get_spotify_client(current_user.id)
        
        # Verificar si el usuario está autenticado
        token_info = sp.auth_manager.get_cached_token()
        if not token_info:
            return jsonify({
                "error": "not_authenticated",
                "message": "Necesitas conectar tu cuenta de Spotify primero"
            }), 401
        
        # Buscar en Spotify
        results = sp.search(q=query, type='track', limit=20)
        tracks = []
        
        for item in results['tracks']['items']:
            track_id = item['id']
            
            # Valores por defecto
            bpm = 122
            key_camelot = "8A"
            energy = 5
            
            try:
                # Obtener audio_features (ahora SÍ funciona con OAuth)
                audio_features = sp.audio_features([track_id])[0]
                
                if audio_features:
                    # BPM real
                    bpm = round(audio_features['tempo'])
                    
                    # Key real (convertir a Camelot)
                    spotify_key = audio_features['key']
                    spotify_mode = audio_features['mode']
                    key_camelot = convert_spotify_key(spotify_key, spotify_mode)
                    
                    # Energy real
                    energy = round(audio_features['energy'] * 10)
                    
                    print(f"✅ {item['name']} - BPM:{bpm} Key:{key_camelot}")
                    
            except Exception as audio_error:
                print(f"⚠️ Error en audio_features: {audio_error}")
            
            track_data = {
                "spotify_id": track_id,
                "artist": item['artists'][0]['name'],
                "track": item['name'],
                "album": item['album']['name'],
                "image": item['album']['images'][0]['url'] if item['album']['images'] else None,
                "preview_url": item['preview_url'],
                "bpm": bpm,
                "key": key_camelot,
                "energy": energy,
            }
            tracks.append(track_data)
        
        return jsonify({"tracks": tracks}), 200
    
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 401:
            return jsonify({
                "error": "not_authenticated",
                "message": "Tu sesión de Spotify expiró. Volvé a conectar."
            }), 401
        return jsonify({"error": str(e)}), 500
    
    except Exception as e:
        print(f"❌ Error en Spotify search: {e}")
        return jsonify({"error": str(e)}), 500


def convert_spotify_key(key_number, mode):
    """Convierte key de Spotify (0-11) a Camelot."""
    camelot_minor = {
        0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
        6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A"
    }
    
    camelot_major = {
        0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
        6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B"
    }
    
    if mode == 0:  # Minor
        return camelot_minor.get(key_number, "8A")
    else:  # Major
        return camelot_major.get(key_number, "8B")

from io import BytesIO
import base64

@app.route("/api/get_energy_data", methods=["POST"])
@login_required
def get_energy_data():
    """Retorna datos para el gráfico de energía."""
    data = request.json or {}
    setlist = data.get("setlist", [])
    
    if not setlist:
        return jsonify({"error": "No setlist provided"}), 400
    
    # Mapear energy levels
    energy_map = {
        "warmup": 3,
        "build": 5,
        "mid_peak": 7,
        "peak_time": 9,
        "peaktime": 9,
        "driving": 10,
        "closing": 6
    }
    
    labels = []
    energy_values = []
    
    for i, track in enumerate(setlist, 1):
        stage = track.get("stage", "warmup").lower()
        energy = energy_map.get(stage, 5)
        
        labels.append(f"#{i}")
        energy_values.append(energy)
    
    return jsonify({
        "labels": labels,
        "data": energy_values
    }), 200


@app.route("/api/get_key_wheel_data", methods=["POST"])
@login_required
def get_key_wheel_data():
    """Retorna datos para el Key Wheel."""
    data = request.json or {}
    setlist = data.get("setlist", [])
    
    if not setlist:
        return jsonify({"error": "No setlist provided"}), 400
    
    # Contar frecuencia de cada key
    key_counts = {}
    key_sequence = []
    
    for track in setlist:
        key = track.get("key", "8A")
        key_sequence.append(key)
        key_counts[key] = key_counts.get(key, 0) + 1
    
    # Todas las keys del Camelot Wheel
    all_keys = [
        "12A", "1A", "2A", "3A", "4A", "5A", "6A", "7A", "8A", "9A", "10A", "11A",
        "12B", "1B", "2B", "3B", "4B", "5B", "6B", "7B", "8B", "9B", "10B", "11B"
    ]
    
    return jsonify({
        "key_counts": key_counts,
        "key_sequence": key_sequence,
        "all_keys": all_keys
    }), 200

@app.route("/api/submit_crypto_payment", methods=["POST"])
@login_required
def submit_crypto_payment():
    """Usuario envía comprobante de pago USDT."""
    data = request.json or {}
    plan = data.get("plan")  # 'monthly' o 'annual'
    tx_id = data.get("tx_id", "")
    screenshot = data.get("screenshot")
    
    if not plan or plan not in ['monthly', 'annual']:
        return jsonify({"error": "Plan inválido"}), 400
    
    if not screenshot:
        return jsonify({"error": "Falta la captura de pantalla"}), 400
    
    # Determinar monto
    amount = 10.0 if plan == 'monthly' else 45.0
    
    # Crear solicitud de pago
    payment_request = PaymentRequest(
        user_id=current_user.id,
        plan=plan,
        amount_usdt=amount,
        tx_id=tx_id,
        screenshot_data=screenshot,
        status='pending'
    )
    
    db.session.add(payment_request)
    db.session.commit()
    
    app.logger.info(f"✅ Nueva solicitud de pago USDT: User {current_user.email}, Plan {plan}, Amount {amount} USDT")
    
    return jsonify({
        "success": True,
        "message": "Comprobante enviado correctamente. Será revisado en 24hs."
    }), 200


@app.route("/admin")
@login_required
def admin_panel():
    """Panel de administración para aprobar pagos."""
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "default@example.com")
    
    # DEBUG
    print(f"🔍 Usuario accediendo: {current_user.email}")
    print(f"🔍 Admin email configurado: {ADMIN_EMAIL}")
    print(f"🔍 ¿Es admin? {current_user.email == ADMIN_EMAIL}")
    
    if current_user.email != ADMIN_EMAIL:
        return "❌ Acceso denegado", 403
    
    # Obtener solicitudes pendientes
    pending_requests = PaymentRequest.query.filter_by(status='pending').order_by(PaymentRequest.created_at.desc()).all()
    
    # Obtener historial (últimas 50)
    history = PaymentRequest.query.filter(PaymentRequest.status.in_(['approved', 'rejected'])).order_by(PaymentRequest.processed_at.desc()).limit(50).all()
    
    return render_template('admin.html', pending=pending_requests, history=history)
@app.route("/admin/approve/<int:request_id>", methods=["POST"])
@login_required
def approve_payment(request_id):
    """Aprobar solicitud de pago."""
    ADMIN_EMAIL = "tu_email@ejemplo.com"  # ← CAMBIÁ ESTO POR TU EMAIL
    
    if current_user.email != ADMIN_EMAIL:
        return jsonify({"error": "No autorizado"}), 403
    
    payment_req = PaymentRequest.query.get(request_id)
    if not payment_req:
        return jsonify({"error": "Solicitud no encontrada"}), 404
    
    if payment_req.status != 'pending':
        return jsonify({"error": "Esta solicitud ya fue procesada"}), 400
    
    # Activar usuario PRO
    user = payment_req.user
    user.role = 'owner'
    
    if payment_req.plan == 'monthly':
        user.pro_until = datetime.utcnow() + timedelta(days=30)
        user.plan = 'monthly'
    else:
        user.pro_until = datetime.utcnow() + timedelta(days=365)
        user.plan = 'annual'
    
    # Marcar solicitud como aprobada
    payment_req.status = 'approved'
    payment_req.processed_at = datetime.utcnow()
    
    db.session.commit()
    
    app.logger.info(f"✅ Pago aprobado: User {user.email}, Plan {payment_req.plan}")
    
    return jsonify({"success": True, "message": f"Usuario {user.email} activado como PRO"}), 200


@app.route("/admin/reject/<int:request_id>", methods=["POST"])
@login_required
def reject_payment(request_id):
    """Rechazar solicitud de pago."""
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "default@example.com")
    
    if current_user.email != ADMIN_EMAIL:
        return jsonify({"error": "No autorizado"}), 403
    
    payment_req = PaymentRequest.query.get(request_id)
    if not payment_req:
        return jsonify({"error": "Solicitud no encontrada"}), 404
    
    if payment_req.status != 'pending':
        return jsonify({"error": "Esta solicitud ya fue procesada"}), 400
    
    payment_req.status = 'rejected'
    payment_req.processed_at = datetime.utcnow()
    
    db.session.commit()
    
    app.logger.info(f"❌ Pago rechazado: User {payment_req.user.email}")
    
    return jsonify({"success": True, "message": "Solicitud rechazada"}), 200

@app.route("/make_me_owner_secret_route_12345")
@login_required
def make_me_owner():
    """Ruta temporal para hacerte owner"""
    if current_user.email == "elidj269@gmail.com":  # ← CAMBIÁ ESTO POR TU EMAIL REAL
        current_user.role = 'owner'
        current_user.pro_until = datetime.utcnow() + timedelta(days=365)
        current_user.plan = 'annual'
        db.session.commit()
        return "✅ Ahora sos OWNER!"
    return "❌ No autorizado"

if __name__ == "__main__":
    with app.app_context():
        db.create_all() 

    app.run(debug=True, port=5000)








