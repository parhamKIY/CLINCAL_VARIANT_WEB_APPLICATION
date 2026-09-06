# راهنمای استقرار سرور با داکر (Docker Deployment Guide)

این راهنما برای استقرار، اجرا و نگهداری برنامه **تفسیر بالینی واریانت‌های ژنتیکی (Clinical Variant Interpretation App)** بر روی سرور با استفاده از Docker و Docker Compose آماده شده است.

---

## ۱. پیش‌نیازهای سرور (System Requirements)

- **سیستم‌عامل:** لینوکس (Ubuntu 20.04/22.04/24.04 LTS، Debian 11/12 یا هر توزیع دیگر)
- **ابزارها:** 
  - Docker Engine نسخه ۲۴.۰ به بالا
  - Docker Compose نسخه ۲ (دستور `docker compose`)
- **سخت‌افزار پیشنهادی:**
  - **RAM:** حداقل ۴ گیگابایت (پیشنهادی ۸ گیگابایت)
  - **CPU:** حداقل ۲ هسته
  - **فضای دیسک آزاد:** حداقل ۵ گیگابایت (جهت ذخیره ایمیج، کش شواهد و فایل‌های آنتولوژی)

---

## ۲. مراحل استقرار گام‌به‌گام (Step-by-Step Setup)

### گام اول: انتقال سورس کد به سرور
پروژه را از طریق Git کلون کنید یا فایل فشرده آن را روی سرور اکسترکت نمایید:

```bash
# در صورت استفاده از گیت:
git clone <REPOSITORY_URL> clinical_variant_app
cd clinical_variant_app
```

### گام دوم: ساخت و تنظیم فایل `.env`
برنامه برای اتصال به مدل زبانی (LLM) و پایگاه‌های شواهد نیاز به تنظیمات محیطی دارد. یک نمونه آماده در فایل `.env.example` قرار دارد:

```bash
# کپی فایل نمونه
cp .env.example .env

# ویرایش فایل با ویرایشگر دلخواه (مثلاً nano)
nano .env
```

**تنظیمات کلیدی که باید در `.env` مقداردهی شوند:**
```ini
# ۱. مشخصات ارائه‌دهنده مدل زبانی (LLM)
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.openai.com/v1          # یا آدرس اندپوینت سرور محلی/ارائه‌دهنده شما
LLM_API_KEY=sk-proj-xxxxxxxxxxxxxxxxxxxx        # کلید API معتبر
LLM_MODEL=gpt-4o-mini                           # یا هر مدل مدنظر

# ۲. نام مدل‌ها برای مراحل مختلف (می‌تواند همان LLM_MODEL باشد)
PHENOTYPE_EXTRACTION_MODEL=gpt-4o-mini
VARIANT_INTERPRETATION_MODEL=gpt-4o-mini
```

> **نکته:** سایر مقادیر پیش‌فرض فایل `.env.example` (پایگاه‌های gnomAD, VEP, ClinVar, HPO و مسیرهای دیتابیس) از پیش بهینه تنظیم شده‌اند و نیازی به تغییر دستی ندارند مگر اینکه نیاز به شخصی‌سازی خاصی داشته باشید.

---

### گام سوم: ساخت و اجرای کانتینر
با اجرای دستور زیر، ایمیج ساخته شده و کانتینر در پس‌زمینه (`detached mode`) اجرا می‌شود:

```bash
docker compose up -d --build
```

### گام چهارم: بررسی وضعیت و مشاهده لاگ‌ها
برای اطمینان از سلامت کانتینر:

```bash
# مشاهده وضعیت سلامت (باید Status به صورت healthy یا Up باشد)
docker compose ps

# مشاهده لاگ‌های برنامه
docker compose logs -f
```

---

## ۳. دسترسی به سامانه

برنامه روی پورت **8501** فعال می‌شود. در مرورگر خود آدرس زیر را وارد کنید:

```text
http://<SERVER_IP>:8501
```
*(به جای `<SERVER_IP>`، آدرس IP عمومی یا محلی سرور را قرار دهید)*

> **نکته فایروال:** اگر برنامه در مرورگر لود نشد، مطمئن شوید پورت 8501 در فایروال سرور باز است:
> ```bash
> # در اوبونتو (UFW):
> sudo ufw allow 8501/tcp
> ```

---

## ۴. پایداری داده‌ها و بک‌آپ (Data Persistence & Backups)

داده‌های حساس و پایگاه‌های داده روی هاست (بیرون کانتینر) به صورت Volume نگهداری می‌شوند:
- `./storage/database/`: پایگاه داده تحلیلی، لاگ‌های تغییرات و سوابق نهایی بیماران
- `./storage/evidence_repository/`: کش دائم و مشترک شواهد بیوانفورماتیک (کاهش فراخوانی APIها)
- `./storage/reports/`: فایل‌های Word (`.docx`) تولید شده برای بیماران
- `./storage/logs/`: لاگ‌های امنیتی و عملیاتی پایپ‌لاین
- `./data/`: کش آنتولوژی و فایل‌های آفلاین HPO

### روش پشتیبان‌گیری سریع (Backup):
برای تهیه بک‌آپ از تمام سوابق، گزارش‌ها و دیتابیس‌ها کافیست پوشه `storage` را فشرده کنید:
```bash
tar -czvf clinical_backup_$(date +%F).tar.gz storage/
```

---

## ۵. دستورات مدیریتی متداول (Common Operations)

| دستور | کاربرد |
| :--- | :--- |
| `docker compose up -d` | شروع اجرای سرویس در پس‌زمینه |
| `docker compose down` | توقف و پاکسازی کانتینرها (بدون حذف داده‌های ذخیره شده) |
| `docker compose restart` | راه‌اندازی مجدد برنامه |
| `docker compose logs -f` | مشاهده زنده لاگ‌های خروجی |
| `docker compose ps` | مشاهده وضعیت کانتینر و بررسی Healthcheck |
| `git pull && docker compose up -d --build` | به‌روزرسانی کدها به آخرین نسخه و بازسازی ایمیج |

---

## ۶. تنظیم دامنه و SSL با Nginx Reverse Proxy (اختیاری)

برای استفاده از دامنه دانشگاهی با پورت 80 / 443 و پروتکل امن HTTPS، یک نمونه کانفیگ Nginx به شکل زیر است (توجه داشته باشید که Streamlit برای انتقال داده‌ها به WebSockets نیاز دارد):

```nginx
server {
    listen 80;
    server_name clinical.your-university.ac.ir;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}
```

---

## ۷. عیب‌یابی مشکلات متداول (Troubleshooting)

1. **خطای دسترسی به پورت (Port already allocated):**
   - اگر پورت 8501 توسط برنامه دیگری اشغال شده باشد، در فایل `docker-compose.yml` خط پورت را تغییر دهید:
     ```yaml
     ports:
       - "8080:8501" # برنامه روی پورت 8080 سرور باز می‌شود
     ```
2. **برنامه در مرحله اتصال به مدل زبانی خطا می‌دهد:**
   - با دستور `docker compose logs -f` لاگ را مشاهده کنید.
   - مطمئن شوید متغیرهای `LLM_API_KEY` و `LLM_BASE_URL` در فایل `.env` صحیح هستند و سرور به آدرس API دسترسی اینترنت دارد.
3. **ریست دسترسی کانتینر به داده‌ها:**
   - اسکریپت `entrypoint.sh` به صورت خودکار در زمان استارت، دسترسی پوشه `storage` را برای کاربر امن داخل کانتینر تنظیم می‌کند.
