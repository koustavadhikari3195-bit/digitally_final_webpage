# Smritikana Business Solutions — Web Application

> One-window solution for company registration, tax compliance, and legal services across India.

---

## 🗂️ Project Structure

```
Smritikana/
├── backend/
│   ├── app.py              # Flask backend (API + SPA serving)
│   ├── requirements.txt    # Python dependencies
│   ├── wsgi.py             # Gunicorn entry point
│   ├── .env.example        # Environment variable template
│   └── public/
│       ├── index.html      # Main single-page application
│       ├── favicon.png     # Site favicon
│       ├── robots.txt      # SEO crawler rules
│       └── sitemap.xml     # SEO sitemap
├── Procfile                # Render/Heroku deployment config
├── .gitignore
└── README.md
```

---

## ⚡ Quick Start (Local Development)

### Prerequisites
- Python 3.10+
- MongoDB (local or Atlas free tier)

### 1. Clone & Install
```bash
git clone <your-repo-url>
cd Smritikana

# Create virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

# Install dependencies
pip install -r backend/requirements.txt
```

### 2. Configure Environment
```bash
copy backend\.env.example backend\.env
# Edit backend\.env and fill in your values
```

### 3. Run the Server
```bash
cd backend
python app.py
```

The app will be available at **http://localhost:5000**

---

## 🌐 Deployment (Vercel — Free Tier)

This application is fully configured to deploy serverlessly on Vercel, combining Python (Flask API + Admin) and static HTML.

### Step 1: MongoDB Atlas
1. Go to [mongodb.com/atlas](https://www.mongodb.com/atlas/database) → Create free M0 cluster
2. Create a database user with read/write access
3. Allow access from anywhere (0.0.0.0/0) in Network Access
4. Copy the connection string → use it as `MONGODB_URI`

### Step 2: Push to GitHub
1. Commit all your latest changes:
   ```bash
   git add .
   git commit -m "Ready for Vercel"
   git push origin main
   ```

### Step 3: Deploy on Vercel
You can deploy directly through the Vercel Dashboard (connecting your GitHub repository), or use the Vercel CLI:

1. Install the CLI: `npm i -g vercel`
2. Run the deployment command from the project root (`f:\Smritikana`):
   ```bash
   vercel
   ```
3. Answer the prompts (Yes to everything, keep defaults).
4. Go to your Vercel Dashboard, select the new project, go to **Settings > Environment Variables**, and add your secrets:
   - `MONGODB_URI` (your production Atlas URL)
   - `ADMIN_SECRET` (a strong password for your admin dashboard)
   - `EMAIL_USER` & `EMAIL_PASS` (for notifications)
5. Redeploy your project from the Vercel dashboard to apply the environment variables!

---

## 🔌 API Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/api/health` | Server health check | Public |
| GET | `/api/stocks` | All cached stock data | Public |
| GET | `/api/stocks/<category>` | Stocks by category (indices/india/crypto) | Public |
| GET | `/api/stocks/refresh` | Force-refresh stock cache | Public |
| POST | `/api/leads` | Submit consultation request | Public |
| GET | `/api/admin/leads` | List consultation leads | Admin |
| PATCH | `/api/admin/leads/<id>` | Update lead status | Admin |

### Admin Authentication
All admin endpoints require the `x-admin-secret` header:
```
x-admin-secret: your-admin-secret-from-env
```

---

## 🔧 Environment Variables

See `backend/.env.example` for the full list and descriptions.

| Variable | Required | Description |
|----------|----------|-------------|
| `MONGODB_URI` | Yes | MongoDB connection string |
| `ADMIN_SECRET` | Yes | Secret for admin API endpoints |
| `EMAIL_USER` | No | Gmail address for email notifications |
| `EMAIL_PASS` | No | Gmail App Password |
| `NOTIFY_EMAIL` | No | Email to receive lead notifications |
| `PORT` | No | Server port (default: 5000) |
| `FRONTEND_URL` | No | Allowed CORS origin |
| `STOCK_REFRESH_SECS` | No | Stock cache refresh interval (default: 60s) |

---

## 📋 Post-Deployment Checklist

- [ ] Replace `TODO: PHONE` placeholders in `index.html` with real numbers
- [ ] Replace `TODO: WHATSAPP` with real WhatsApp number in `index.html`
- [ ] Verify contact form sends email (submit a test lead)
- [ ] Verify `/api/health` shows `"mongo": "connected"`
- [ ] Verify stock ticker shows live data (not "Simulated")
- [ ] Add Google Analytics GA4 tag to `index.html`
- [ ] Submit `sitemap.xml` to Google Search Console

---

## 📄 License

© 2025 Smritikana Business Solutions. All Rights Reserved.
