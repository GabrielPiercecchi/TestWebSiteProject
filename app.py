import os
import secrets
import smtplib
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from PIL import Image
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

try:
    import stripe
except ImportError:
    stripe = None

load_dotenv()

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=["120 per minute"])
PASSWORD_HASH_METHOD = "scrypt"


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="guest", nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    bookings = db.relationship("Booking", backref="guest", lazy=True, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method=PASSWORD_HASH_METHOD)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.enabled

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_staff(self):
        return self.role in {"admin", "staff"}


class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(300), nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    image_url = db.Column(db.String(500), nullable=True)
    bookings = db.relationship("Booking", backref="room", lazy=True)


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    check_in = db.Column(db.Date, nullable=False)
    check_out = db.Column(db.Date, nullable=False)
    guests = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    payment_status = db.Column(db.String(20), default="unpaid", nullable=False)
    stripe_session_id = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)


class PasswordReset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


def create_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///hotel.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        WTF_CSRF_TIME_LIMIT=3600,
        TRUSTED_HOSTS=[host.strip() for host in os.environ.get("TRUSTED_HOSTS", "localhost,127.0.0.1").split(",")],
        STRIPE_SECRET_KEY=os.environ.get("STRIPE_SECRET_KEY", ""),
        STRIPE_PUBLISHABLE_KEY=os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        MAIL_SERVER=os.environ.get("MAIL_SERVER", ""),
        MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
        MAIL_USERNAME=os.environ.get("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
        MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "1") == "1",
        MAIL_FROM=os.environ.get("MAIL_FROM", "noreply@gabriels-hotel.local"),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
        UPLOAD_FOLDER=os.environ.get("UPLOAD_FOLDER", "/app/uploads"),
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"
    csrf.init_app(app)
    limiter.init_app(app)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https://images.unsplash.com data:; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com"
        return response

    with app.app_context():
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        db.create_all()
        seed_data(app)

    @app.route("/")
    def index():
        rooms = Room.query.order_by(Room.price).all()
        return render_template("index.html", rooms=rooms)

    @app.route("/register", methods=["GET", "POST"])
    @limiter.limit("5 per minute")
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            password_confirmation = request.form.get("password_confirmation", "")
            if len(name) < 2 or len(password) < 10 or "@" not in email:
                flash("Inserisci dati validi e una password di almeno 10 caratteri.", "error")
            elif password != password_confirmation:
                flash("Le due password non coincidono.", "error")
            elif User.query.filter_by(email=email).first():
                flash("Questa email è già registrata.", "error")
            else:
                user = User(name=name, email=email)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                login_user(user)
                return redirect(url_for("dashboard"))
        return render_template("auth.html", mode="register")

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("5 per minute")
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            user = User.query.filter_by(email=request.form.get("email", "").strip().lower()).first()
            if user and user.enabled and user.check_password(request.form.get("password", "")):
                login_user(user)
                return redirect(url_for("admin" if user.is_staff else "dashboard"))
            flash("Email o password non validi.", "error")
        return render_template("auth.html", mode="login")

    @app.route("/forgot-password", methods=["GET", "POST"])
    @limiter.limit("3 per hour")
    def forgot_password():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            user = User.query.filter_by(email=email).first()
            if user:
                raw_token = secrets.token_urlsafe(32)
                reset = PasswordReset(
                    token_hash=hash_token(raw_token),
                    expires_at=datetime.utcnow() + timedelta(minutes=30),
                    user_id=user.id,
                )
                db.session.add(reset)
                db.session.commit()
                reset_url = url_for("reset_password", token=raw_token, _external=True)
                send_reset_email(app, user.email, reset_url)
            flash("Se l'email è registrata, riceverai un link per reimpostare la password.", "success")
            return redirect(url_for("login"))
        return render_template("auth.html", mode="forgot")

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    @limiter.limit("5 per hour")
    def reset_password(token):
        reset = PasswordReset.query.filter_by(token_hash=hash_token(token), used_at=None).first()
        if not reset or reset.expires_at < datetime.utcnow():
            flash("Il link di recupero non è valido o è scaduto.", "error")
            return redirect(url_for("forgot_password"))
        if request.method == "POST":
            password = request.form.get("password", "")
            confirmation = request.form.get("password_confirmation", "")
            if len(password) < 10 or password != confirmation:
                flash("La password deve avere almeno 10 caratteri e coincidere con la conferma.", "error")
            else:
                user = db.session.get(User, reset.user_id)
                user.set_password(password)
                reset.used_at = datetime.utcnow()
                db.session.commit()
                flash("Password aggiornata. Ora puoi accedere.", "success")
                return redirect(url_for("login"))
        return render_template("auth.html", mode="reset", reset_token=token)

    @app.post("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("index"))

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    @limiter.limit("10 per minute")
    def profile():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            new_password = request.form.get("new_password", "")
            password_confirmation = request.form.get("password_confirmation", "")
            other_user = User.query.filter(User.email == email, User.id != current_user.id).first()
            if len(name) < 2 or len(name) > 120 or "@" not in email:
                flash("Nome o email non validi.", "error")
            elif other_user:
                flash("Questa email è già utilizzata.", "error")
            elif bool(new_password or password_confirmation) and (len(new_password) < 10 or new_password != password_confirmation):
                flash("Per cambiare password inserisci una nuova password di almeno 10 caratteri e una conferma identica.", "error")
            else:
                current_user.name = name
                current_user.email = email
                if new_password:
                    current_user.set_password(new_password)
                db.session.commit()
                flash("Profilo aggiornato.", "success")
                return redirect(url_for("profile"))
        return render_template("profile.html")

    @app.route("/book", methods=["GET", "POST"])
    @login_required
    @limiter.limit("10 per minute")
    def book():
        rooms = Room.query.order_by(Room.price).all()
        if request.method == "POST":
            try:
                check_in = date.fromisoformat(request.form["check_in"])
                check_out = date.fromisoformat(request.form["check_out"])
                guests = int(request.form["guests"])
                room = db.session.get(Room, int(request.form["room_id"]))
            except (KeyError, TypeError, ValueError):
                room = None
                check_in = check_out = None
                guests = 0
            conflict = None
            if room and check_in and check_out:
                conflict = Booking.query.filter(Booking.room_id == room.id, Booking.status.in_(["pending", "confirmed"]), Booking.check_in < check_out, Booking.check_out > check_in).first()
            if not room or not check_in or not check_out or check_in < date.today() or check_out <= check_in or guests < 1 or guests > room.capacity or conflict:
                flash("Date, camera o numero di ospiti non disponibili.", "error")
            else:
                booking = Booking(check_in=check_in, check_out=check_out, guests=guests, user_id=current_user.id, room_id=room.id)
                db.session.add(booking)
                db.session.commit()
                if app.config["STRIPE_SECRET_KEY"] and stripe:
                    stripe.api_key = app.config["STRIPE_SECRET_KEY"]
                    nights = (check_out - check_in).days
                    checkout = stripe.checkout.Session.create(
                        mode="payment",
                        line_items=[{"price_data": {"currency": "eur", "product_data": {"name": room.name}, "unit_amount": int(room.price * 100)}, "quantity": nights}],
                        success_url=url_for("payment_success", booking_id=booking.id, _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
                        cancel_url=url_for("payment_cancel", booking_id=booking.id, _external=True),
                        customer_email=current_user.email,
                        metadata={"booking_id": str(booking.id)},
                    )
                    booking.stripe_session_id = checkout.id
                    db.session.commit()
                    return redirect(checkout.url)
                booking.status = "confirmed"
                booking.payment_status = "demo_paid"
                db.session.commit()
                flash("Prenotazione confermata in modalità demo.", "success")
                return redirect(url_for("dashboard"))
        return render_template("book.html", rooms=rooms, today=date.today().isoformat(), availability=availability_payload())

    @app.get("/api/availability")
    @limiter.limit("30 per minute")
    def availability():
        check_in = parse_optional_date(request.args.get("check_in"))
        check_out = parse_optional_date(request.args.get("check_out"))
        guests = parse_positive_int(request.args.get("guests"))
        rooms = available_rooms(check_in, check_out, guests)
        room_id = parse_positive_int(request.args.get("room_id"))
        bookings = []
        if room_id:
            bookings = [{"check_in": booking.check_in.isoformat(), "check_out": booking.check_out.isoformat()} for booking in Booking.query.filter_by(room_id=room_id).filter(Booking.status.in_(["pending", "confirmed"])).all()]
        return jsonify({"rooms": [room_summary(room) for room in rooms], "bookings": bookings})

    @app.get("/payment/success/<int:booking_id>")
    @login_required
    def payment_success(booking_id):
        booking = db.session.get(Booking, booking_id)
        if not booking or booking.user_id != current_user.id:
            flash("Prenotazione non trovata.", "error")
        elif not app.config["STRIPE_SECRET_KEY"] or not stripe or not request.args.get("session_id"):
            flash("Non è stato possibile verificare il pagamento.", "error")
        else:
            stripe.api_key = app.config["STRIPE_SECRET_KEY"]
            checkout = stripe.checkout.Session.retrieve(request.args["session_id"])
            if checkout.id != booking.stripe_session_id or checkout.payment_status != "paid":
                flash("Il pagamento non risulta completato.", "error")
            else:
                booking.status = "confirmed"
                booking.payment_status = "paid"
                db.session.commit()
                flash("Pagamento completato e soggiorno confermato.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/payment/cancel/<int:booking_id>")
    @login_required
    def payment_cancel(booking_id):
        booking = db.session.get(Booking, booking_id)
        if booking and booking.user_id == current_user.id:
            booking.status = "cancelled"
            booking.payment_status = "cancelled"
            db.session.commit()
        flash("Pagamento annullato: la camera è stata liberata.", "error")
        return redirect(url_for("book"))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        bookings = Booking.query.filter_by(user_id=current_user.id).order_by(Booking.check_in.desc()).all()
        return render_template("dashboard.html", bookings=bookings)

    @app.post("/booking/<int:booking_id>/cancel")
    @login_required
    @limiter.limit("20 per minute")
    def cancel_booking(booking_id):
        if not current_user.is_admin:
            flash("Solo l'amministratore può annullare prenotazioni.", "error")
            return redirect(url_for("dashboard"))
        booking = db.session.get(Booking, booking_id)
        if not booking or (booking.user_id != current_user.id and not current_user.is_admin):
            flash("Prenotazione non trovata.", "error")
        else:
            booking.status = "cancelled"
            booking.payment_status = "cancelled"
            db.session.commit()
            flash("Prenotazione annullata.", "success")
        return redirect(url_for("admin" if current_user.is_admin else "dashboard"))

    def staff_required(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not current_user.is_staff:
                flash("Accesso riservato allo staff.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped

    def admin_required(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not current_user.is_admin:
                flash("Questa funzione è riservata all'amministratore.", "error")
                return redirect(url_for("admin"))
            return view(*args, **kwargs)
        return wrapped

    @app.get("/admin")
    @staff_required
    def admin():
        bookings = Booking.query.order_by(Booking.created_at.desc()).all()
        return render_template("admin.html", bookings=bookings, rooms=Room.query.order_by(Room.name).all(), staff_users=User.query.filter_by(role="staff").order_by(User.name).all())

    @app.post("/admin/staff/<int:user_id>/toggle")
    @admin_required
    def toggle_staff(user_id):
        user = db.session.get(User, user_id)
        if not user or user.role != "staff":
            flash("Utenza STAFF non trovata.", "error")
        else:
            user.enabled = not user.enabled
            db.session.commit()
            flash("Utenza STAFF " + ("riattivata." if user.enabled else "disabilitata."), "success")
        return redirect(url_for("admin"))

    @app.post("/admin/staff/<int:user_id>/delete")
    @admin_required
    def delete_staff(user_id):
        user = db.session.get(User, user_id)
        if not user or user.role != "staff":
            flash("Utenza STAFF non trovata.", "error")
        else:
            db.session.delete(user)
            db.session.commit()
            flash("Utenza STAFF cancellata.", "success")
        return redirect(url_for("admin"))

    @app.route("/admin/rooms/new", methods=["GET", "POST"])
    @staff_required
    def new_room():
        room = Room()
        if request.method == "POST" and save_room(room, app):
            db.session.add(room)
            db.session.commit()
            flash("Camera aggiunta.", "success")
            return redirect(url_for("admin"))
        return render_template("room_form.html", room=room, mode="new")

    @app.route("/admin/rooms/<int:room_id>/edit", methods=["GET", "POST"])
    @staff_required
    def edit_room(room_id):
        room = db.session.get(Room, room_id)
        if not room:
            flash("Camera non trovata.", "error")
            return redirect(url_for("admin"))
        if request.method == "POST" and save_room(room, app):
            db.session.commit()
            flash("Camera aggiornata.", "success")
            return redirect(url_for("admin"))
        return render_template("room_form.html", room=room, mode="edit")

    @app.route("/admin/users/staff/new", methods=["GET", "POST"])
    @admin_required
    def new_staff():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            confirmation = request.form.get("password_confirmation", "")
            if len(name) < 2 or "@" not in email or len(password) < 10 or password != confirmation:
                flash("Inserisci dati validi e due password uguali di almeno 10 caratteri.", "error")
            elif User.query.filter_by(email=email).first():
                flash("Questa email è già registrata.", "error")
            else:
                user = User(name=name, email=email, role="staff")
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash("Utenza STAFF creata.", "success")
                return redirect(url_for("admin"))
        return render_template("staff_form.html")

    return app


@login_manager.user_loader
def load_user(user_id):
    user = db.session.get(User, int(user_id))
    return user if user and user.enabled else None


def seed_data(app):
    if Room.query.count() == 0:
        db.session.add_all([
            Room(name="Camera Alba", description="Luce naturale, letto king e vista sul giardino.", price=145, capacity=2, image_url="/static/rooms/alba.svg"),
            Room(name="Suite Gabriel", description="Spazi ampi, salotto privato e terrazza panoramica.", price=260, capacity=4, image_url="/static/rooms/suite-gabriel.svg"),
            Room(name="Camera Oliva", description="Un rifugio raccolto con dettagli artigianali.", price=180, capacity=3, image_url="/static/rooms/oliva.svg"),
        ])
    else:
        default_images = {
            "Camera Alba": "/static/rooms/alba.svg",
            "Suite Gabriel": "/static/rooms/suite-gabriel.svg",
            "Camera Oliva": "/static/rooms/oliva.svg",
        }
        for room in Room.query.all():
            if not room.image_url and room.name in default_images:
                room.image_url = default_images[room.name]
    admin_email = os.environ.get("ADMIN_EMAIL")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if admin_email and admin_password and not User.query.filter_by(email=admin_email).first():
        admin = User(name="Gabriel's Hotel Staff", email=admin_email, role="admin")
        admin.set_password(admin_password)
        db.session.add(admin)
    demo_accounts = [
        (os.environ.get("STAFF_EMAIL"), os.environ.get("STAFF_PASSWORD"), "Elena Rossi", "staff"),
        (os.environ.get("DEMO_USER_EMAIL"), os.environ.get("DEMO_USER_PASSWORD"), "Marco Bianchi", "guest"),
    ]
    for email, password, name, role in demo_accounts:
        if email and password and not User.query.filter_by(email=email).first():
            demo_user = User(name=name, email=email, role=role)
            demo_user.set_password(password)
            db.session.add(demo_user)
    db.session.commit()


def hash_token(token):
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def send_reset_email(app, recipient, reset_url):
    message = EmailMessage()
    message["Subject"] = "Reimposta la password di Gabriel's Hotel"
    message["From"] = app.config["MAIL_FROM"]
    message["To"] = recipient
    message.set_content(f"Apri questo link entro 30 minuti per impostare una nuova password:\n\n{reset_url}\n\nSe non hai richiesto il reset, ignora questa email.")
    if not app.config["MAIL_SERVER"]:
        app.logger.info("Password reset email for %s: %s", recipient, reset_url)
        return
    try:
        with smtplib.SMTP(app.config["MAIL_SERVER"], app.config["MAIL_PORT"], timeout=15) as smtp:
            if app.config["MAIL_USE_TLS"]:
                smtp.starttls()
            if app.config["MAIL_USERNAME"]:
                smtp.login(app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException):
        app.logger.exception("Unable to send password reset email to %s", recipient)
        return False


def availability_payload():
    rooms = Room.query.order_by(Room.id).all()
    return {str(room.id): [{"check_in": booking.check_in.isoformat(), "check_out": booking.check_out.isoformat()} for booking in Booking.query.filter_by(room_id=room.id).filter(Booking.status.in_(["pending", "confirmed"])).all()] for room in rooms}


def parse_optional_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def parse_positive_int(value):
    try:
        parsed = int(value) if value else None
        return parsed if parsed and parsed > 0 else None
    except (TypeError, ValueError):
        return None


def available_rooms(check_in=None, check_out=None, guests=None):
    rooms = Room.query.order_by(Room.price).all()
    if guests:
        rooms = [room for room in rooms if room.capacity >= guests]
    if check_in and check_out and check_out > check_in:
        conflicts = Booking.query.filter(Booking.status.in_(["pending", "confirmed"]), Booking.check_in < check_out, Booking.check_out > check_in).all()
        blocked_room_ids = {booking.room_id for booking in conflicts}
        rooms = [room for room in rooms if room.id not in blocked_room_ids]
    return rooms


def room_summary(room):
    return {"id": room.id, "name": room.name, "description": room.description, "price": str(room.price), "capacity": room.capacity, "image_url": room.image_url}


def save_room(room, app):
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    price_text = request.form.get("price", "").strip().replace(",", ".")
    capacity_text = request.form.get("capacity", "").strip()
    image = request.files.get("image")
    try:
        price = float(price_text)
        capacity = int(capacity_text)
    except (TypeError, ValueError):
        flash("Prezzo e numero ospiti devono essere numeri validi.", "error")
        return False
    if not 1 <= len(name) <= 120 or not 1 <= len(description) <= 300 or price <= 0 or price > 100000 or not 1 <= capacity <= 50:
        flash("Controlla nome, descrizione, prezzo e capienza della camera.", "error")
        return False
    room.name = name
    room.description = description
    room.price = price
    room.capacity = capacity
    if image and image.filename:
        extension = os.path.splitext(secure_filename(image.filename))[1].lower()
        if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
            flash("Formato immagine non consentito. Usa JPG, PNG o WEBP.", "error")
            return False
        try:
            image.stream.seek(0)
            with Image.open(image.stream) as checked_image:
                checked_image.verify()
            image.stream.seek(0)
        except Exception:
            flash("Il file caricato non è un'immagine valida.", "error")
            return False
        filename = f"{uuid.uuid4().hex}{extension}"
        image.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        room.image_url = f"/media/{filename}"
    return True


app = create_app()
