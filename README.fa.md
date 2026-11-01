# 🧪 SecurAI Prompt Tester

🇬🇧 [English README](README.md)

یک ابزار سریع برای سنجش مقاومت اپ‌های مبتنی بر LLM در برابر prompt injection. آدرس چت اپ را می‌دهی، ابزار ۲۱ پرامپت حمله‌ی امن و شناخته‌شده می‌فرستد و یک گزارش به زبان ساده با امتیاز ۰ تا ۱۰۰ می‌نویسد.

بخشی از **[مجموعه‌ی SecurAI](https://github.com/ElhamBidarigh/securai)**.

## برای چه کسی است

ابزارهای قدرتمندتری مثل garak و Promptfoo و PyRIT وجود دارند و برای محقق امنیت انتخاب درستی‌اند. این ابزار عمداً کوچک‌تر است: برای مؤسس، مدیر محصول یا توسعه‌دهنده‌ای که در ۵ دقیقه یک جواب اولیه و یک گزارش قابل‌فهم برای افراد غیرفنی می‌خواهد. هیچ وابستگی ندارد و جایگزین یک تست نفوذ کامل نیست.

## چه چیزهایی را تست می‌کند

| نوع حمله | تعداد | چه چیزی چک می‌شود |
|---|---|---|
| بازنویسی مستقیم دستور | ۵ | آیا اپ به «همه‌ی دستورهای قبلی را نادیده بگیر» تن می‌دهد؟ |
| تغییر نقش | ۱ | آیا کاربر می‌تواند AI را به شخصیت دیگری تبدیل کند؟ |
| نشت system prompt | ۳ | آیا دستورهای مخفی را لو می‌دهد؟ (نیاز به `--system-marker`) |
| دستور پنهان در محتوا | ۴ | آیا از دستور قایم‌شده در سند، صفحه‌ی وب، ایمیل یا JSON اطاعت می‌کند؟ |
| رمزگذاری و پنهان‌سازی | ۳ | Base64، leetspeak و فاصله‌گذاری حروف |
| زبان‌های دیگر | ۳ | همان حمله به ترکی، عربی و فارسی |
| خروجی ناامن | ۲ | آیا HTML فعال یا تصویر مارک‌داونی که داده را نشت می‌دهد تولید می‌کند؟ |

**همه‌ی پرامپت‌ها بی‌خطرند.** فقط سعی می‌کنند اپ را وادار کنند یک توکن تصادفی بی‌ضرر مثل `SAI-3fa9c1d2` را چاپ کند یا عبارتی از system prompt خودش را تکرار کند. تست نشت داده به `attacker.invalid` اشاره می‌کند که یک دامنه‌ی رزروشده است و هرگز resolve نمی‌شود.

**نتیجه قطعی (deterministic) است.** پاسخ فقط با بررسی دقیق رشته و regex قضاوت می‌شود، نه با یک LLM دیگر.

## نصب و اجرا

```bash
git clone https://github.com/ElhamBidarigh/securAI-prompt-tester.git
cd securAI-prompt-tester
python -m prompt_tester --help
```

پایتون ۳.۱۰ یا بالاتر لازم است. هیچ پکیج اضافه‌ای لازم نیست.

## امتحان در ۳۰ ثانیه (بدون API key)

```bash
python -m prompt_tester demo
```

سه اپ چت ساختگی روی کامپیوتر خودت اجرا می‌شود (یکی آسیب‌پذیر، یکی نیمه‌آسیب‌پذیر، یکی ایمن) و گزارش نمونه در پوشه‌ی `examples/` ساخته می‌شود. این اپ‌ها شبیه‌سازی هستند و مدل واقعی نیستند.

## تست اپ خودت

```bash
python -m prompt_tester scan \
  --url https://your-app.example.com/api/chat \
  --authorized \
  --system-marker "یک عبارت از system prompt واقعی خودت" \
  --out report.md
```

`--authorized` برای هر آدرسی که روی کامپیوتر خودت نیست اجباری است و تأیید می‌کند که صاحب اپ هستی یا اجازه‌ی کتبی داری. فقط سیستم‌هایی را تست کن که اجازه‌اش را داری.

مثال‌های OpenAI و Anthropic و همه‌ی گزینه‌ها در [README انگلیسی](README.md) آمده است.

## تست مستقیم یک مدل واقعی

```bash
export ANTHROPIC_API_KEY="your-key"     # یا OPENAI_API_KEY برای --preset openai
python -m prompt_tester scan --authorized --preset anthropic --model YOUR_MODEL_NAME --repeat 3 --out report.md
```

کلید فقط در همین پنجره‌ی ترمینال می‌ماند و هرگز در گزارش یا خروجی چاپ نمی‌شود. نتیجه درباره‌ی ترکیب «مدل و system prompt ای که تست کردی» است، نه خود مدل به‌طور کلی. جزئیات کامل، خروجی SARIF و GitHub Action در [README انگلیسی](README.md) آمده است.

## محدودیت‌ها

این یک بررسی اولیه است، نه گواهی امنیت. مجموعه‌ی ثابتی از حمله‌های پایه را پوشش می‌دهد، رشته‌ها را تطبیق می‌دهد و معنا را قضاوت نمی‌کند، و مدل‌ها در هر اجرا ممکن است پاسخ متفاوتی بدهند. امتیاز بالا یعنی اپ این حمله‌ها را تحمل کرده، نه اینکه در برابر مهاجم مصمم امن است.

## مجموعه‌ی کامل SecurAI

| ابزار | نوع | لینک |
|---|---|---|
| 🛡️ Audit Tool | خودارزیابی (مرورگر) | [نسخه‌ی زنده](https://elhambidarigh.github.io/securai/) |
| 🔬 Vuln Lab | آموزشی (PHP/MySQL) | [ریپو](https://github.com/ElhamBidarigh/securAI-vuln-lab) |
| 🔐 JWT Analyzer | CLI (پایتون) | [ریپو](https://github.com/ElhamBidarigh/jwt-analyzer) |
| 🔍 Active Scanner | اسکنر URL زنده (PHP) | [ریپو](https://github.com/ElhamBidarigh/securAI-active-scanner) |
| 🔌 MCP Scanner | CLI (پایتون) | [ریپو](https://github.com/ElhamBidarigh/securAI-mcp-scanner) |
| 🧪 Prompt Tester | CLI (پایتون) | همین‌جایی که هستی |

## مجوز

MIT
