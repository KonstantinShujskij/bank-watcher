# bank-watcher

Окремий сервіс, що моніторить збори/банки за посиланням і фіксує зарахування,
відсилаючи підписані колбеки в ncP2P. Перший адаптер — **monobank**, решта банків
підключаються як нові адаптери без змін ядра.

> Частина екосистеми ncp. Архітектурний контекст — у `ncp-knowledge`
> (bank-watcher як зовнішнє джерело транзакцій + підписаний колбек у ncP2P).

## Як працює виявлення зарахувань (важливо)

Публічне API банки віддає лише **агрегати**: усього зібрано (`amount`) та усього
знято (`withdrawal`). **Списку транзакцій і txId немає.** Тому:

- зарахування = **приріст `amount`** між двома опитуваннями (раз на 1с);
- два поповнення в одне секундне вікно **зіллються в одну дельту** — їх не
  роз'єднати (кандидат на ручний розбір на боці ncP2P, не на авто-confirm);
- дедуп — синтетичний `event_id` від кумулятивного балансу (монотонний), тож
  ретраї/рестарти не дублюють зарахування;
- `baseline` фіксується при підписці → вже зібране до підписки не фаєриться.

## Стек

Python 3.9+ · FastAPI · httpx · SQLite (aiosqlite) · cryptography.

## Запуск (локально)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # за потреби відредагуй
uvicorn app.main:app --reload --port 8080
```

Відкрий http://localhost:8080 — базовий фронт (список банок, додавання,
історія зарахувань). Swagger: http://localhost:8080/docs.

## HTTP API

| Метод | Шлях | Призначення |
|---|---|---|
| `GET` | `/api/health` | стан + к-сть активних банок |
| `GET` | `/api/banks` | доступні банк-адаптери |
| `POST` | `/api/jars` | підписатися на банку `{bank,url,card,callback_url}` |
| `GET` | `/api/jars` | список банок |
| `GET` | `/api/jars/{ref}` | одна банка |
| `DELETE` | `/api/jars/{ref}` | прибрати з моніторингу |
| `POST` | `/api/jars/{ref}/pause` · `/resume` | пауза/відновлення |
| `GET` | `/api/jars/{ref}/credits` | **історія зарахувань по збору** |

## Контракт колбеку (до ncP2P)

При виявленні зарахування POST на `callback_url`:

```json
{
  "event_id": "<sha256 hex, idempotency key>",
  "bank": "mono",
  "jar_ref": "9SbRPZB56d",
  "card": "4874100038049642",
  "amount": 70000,
  "currency": "980",
  "balance_after": 770000,
  "detected_at": 1748200000000
}
```

- Суми — у **копійках**.
- Заголовки: `X-Event-Id`, `X-Signature` = `HMAC-SHA256(raw_body, CALLBACK_SECRET)` (hex).
- Доставка з ретраями (outbox): 2xx = `delivered`, інакше повтор до `CALLBACK_MAX_ATTEMPTS`, далі `failed`.
- Отримувач **має бути ідемпотентним по `event_id`**.

> Секрет і точна форма ще не узгоджені з ncP2P — поле `CALLBACK_SECRET` поки порожнє.

## NordVPN (ротація вихідного IP)

Опитування раз на секунду впирається в per-IP ліміти банків, тож вихідний IP
варто ротувати. Кероване через env (`VPN_ENABLED`, `VPN_ROTATE_SECONDS`,
`VPN_COUNTRIES`) поверх NordVPN CLI.

**Split-tunnel:** ротація на мить рве з'єднання. Полл-цикл це переживає, але щоб
не рвати вхідні з'єднання до нашого API/фронта — винеси наш порт із тунелю на сервері:

```bash
nordvpn allowlist add port <PORT>
```

Локально лишай `VPN_ENABLED=false`.

## Конфіг (env)

Усі ключі — в `.env.example`.

## Що далі

- [ ] Узгодити з ncP2P форму колбеку + спільний HMAC-секрет.
- [ ] Підписати вхідні запити `POST /api/jars` (HMAC від ncP2P).
- [ ] Розгортання на окремому сервері + NordVPN split-tunnel.
- [ ] Адаптери інших банків.
