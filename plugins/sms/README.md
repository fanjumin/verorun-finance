# SMS Service (sms)

## Overview

SMS Service is the SMS verification code sending service plugin for the VeroRun platform. It supports two SMS providers — Aliyun and Twilio — and automatically routes between providers based on the phone number's country dial code. The plugin uses a dedicated PostgreSQL schema `sms` to store SMS templates and send logs, and provides verification code generation, sending, phone validation, and send rate limiting.

The plugin also exposes dynamic SMS login/registration methods to the platform through the `get_login_methods` and `get_register_methods` hooks, including country code selection and international phone validation.

## Features

- **Dual provider support**: Aliyun (domestic) and Twilio (international), auto-routed by country dial code
- **Smart routing**: `+86` numbers go to Aliyun, other international dial codes go to Twilio; numbers without a dial code fall back to the `DEPLOY_MARKET` environment variable
- **Verification code generation**: secure random 6-digit codes generated with the `secrets` module
- **Template management**: CRUD management for three SMS template categories — captcha, notice, promo
- **Send logs**: complete record of every send (phone, code, purpose, provider, status)
- **Rate limiting**: hourly send limit tracked in the `sms_rate_limits` table of the plugin schema `sms` (default 5 sends/hour)
- **Phone validation**: validates phone formats for 33 countries/regions with automatic country code detection
- **Country code selection**: complete country list (flag emoji, dial code, CN/EN names)
- **Dynamic login methods**: SMS authentication via the `get_login_methods` / `get_register_methods` hooks
- **Independent database**: PostgreSQL schema `sms` with the `sms_templates`, `sms_logs`, and `sms_rate_limits` tables
- **Data migration**: idempotent migration of SMS template data from the main DB on first startup

## Architecture

```
+--------------------------------------------------------------+
|                 Frontend (Admin Panel + User Side)            |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                     Routing Layer (routes.py)                 |
|  /admin/sms/*                                                 |
|  +-- /templates     SMS template CRUD (grouped by category)  |
|  +-- /logs          SMS send log query                       |
|  +-- /test-send     Test SMS sending                         |
|  +-- /settings      Aliyun config read/write                 |
|  +-- /countries     Country code list                        |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                    Service Layer (services.py)                |
|  +-- send_sms()              SMS send entry point            |
|  +-- generate_code()         Verification code generation    |
|  +-- validate_phone()        Phone validation                |
|  +-- check_rate_limit()      Rate limit check                |
|  +-- get_sms_provider()      Automatic provider selection    |
|  +-- _select_provider_by_phone()  Dial-code routing           |
|  +-- _send_aliyun_via_provider()  Aliyun send via provider   |
|  +-- _log_send()             Send log recording              |
+--------------------------------------------------------------+
              |                           |
              v                           v
+------------------------+    +---------------------------+
|  countries.py          |    |  auth-center providers    |
|  +-- COUNTRIES (33)    |    |  +-- providers/sms/      |
|  +-- find_country()    |    |      aliyun.py           |
|  +-- detect_country()  |    |      twilio.py           |
|  +-- validate_phone()  |    |      (reuse existing Provider)|
+------------------------+    +---------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                     Data Layer (models.py)                    |
|  PG Schema: sms                                               |
|  +-- sms_templates      SMS template table                   |
|  +-- sms_logs           SMS send log table                   |
|  +-- sms_rate_limits    Send rate limit table                |
+--------------------------------------------------------------+
```

**Provider routing logic**:

```
Phone starting with +86 --> Aliyun (AliyunSMSProvider)
Phone starting with + (not +86) --> Twilio (TwilioSMSProvider)
No dial code --> DEPLOY_MARKET=cn ? Aliyun : Twilio
```

## Directory Structure

```
sms/
+-- README.md                    # Plugin documentation
+-- README_CN.md                 # Chinese documentation
+-- plugin.json                  # Plugin metadata
+-- __init__.py                  # Plugin entrypoint, blueprint & hooks registration
+-- models.py                    # Data models (DB connection, table creation, main DB migration)
+-- routes.py                    # Admin API routes (templates, logs, test send, config, countries)
+-- services.py                  # Core services (send, code, phone validation, rate limit, provider routing)
+-- countries.py                 # Country list & phone validation rules
+-- migrations/
|   +-- v1.0.0_to_v1.1.0.sql     # Version migration SQL (§10.6)
+-- i18n/
|   +-- en.yml                   # English i18n
|   +-- zh-CN.yml                # Chinese i18n
+-- templates/
    +-- admin_sms.html           # Admin panel page template
```

## Installation & Enablement

### Prerequisites

- VeroRun platform version >= 0.10.0
- Aliyun SMS AccessKey or Twilio Account SID/Token
- PostgreSQL database

### Installation Steps

1. Place the `sms` directory under `plugins/`
2. Restart the application; the plugin will automatically:
   - Create the PostgreSQL schema `sms`
   - Initialize the `sms_templates`, `sms_logs`, and `sms_rate_limits` tables
   - Idempotently migrate SMS template data from the main DB
3. Configure the Aliyun parameters in Admin Panel > "Security & Compliance" > "SMS Management"
4. Make sure `auth-center`'s `providers/sms/aliyun.py` and `providers/sms/twilio.py` are properly configured

### Environment Variables

| Environment Variable | Description |
|----------------------|-------------|
| `DEPLOY_MARKET` | Deployment market: `cn` (domestic) / `intl` (international), affects provider selection for numbers without a dial code |

## Configuration

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `aliyun_sms_sign_name` | string | "" | Aliyun SMS signature |
| `aliyun_sms_access_key` | string | "" | Aliyun AccessKey ID |
| `aliyun_sms_secret` | string | "" | Aliyun AccessKey Secret (sensitive field) |

## API Endpoints

### Admin API (admin permission required)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/sms/templates` | Get all SMS templates (grouped by category: captcha/notice/promo) |
| POST | `/admin/sms/templates` | Create a new SMS template |
| PUT | `/admin/sms/templates/<id>` | Update an SMS template |
| DELETE | `/admin/sms/templates/<id>` | Delete an SMS template |
| GET | `/admin/sms/logs` | Paginated SMS send log query |
| POST | `/admin/sms/test-send` | Test SMS sending (supports country code selection) |
| GET | `/admin/sms/settings` | Get Aliyun SMS configuration |
| POST | `/admin/sms/settings` | Save Aliyun SMS configuration |
| GET | `/admin/sms/countries` | Get the supported country/region list |

### Test Send Request Body Example

```json
{
  "phone": "13800138000",
  "code": "123456",
  "country_code": "+86",
  "purpose": "test"
}
```

## SMS Template Categories

| Category | Identifier | Description |
|----------|------------|-------------|
| Verification | captcha | Verification code sends for login, registration, password change, etc. |
| Notification | notice | Order notifications, system notifications, etc. |
| Promotional | promo | Marketing/promotional SMS |

**Preset template mapping** (Aliyun template codes):

| Purpose | Template Code |
|---------|---------------|
| register | SMS_506135003 |
| change_phone | SMS_506380001 |
| reset_password | SMS_506285002 |
| modify_password | SMS_506190002 |
| login | SMS_506330002 |

## Dependencies

### Internal Dependencies

| Dependency | Purpose |
|------------|---------|
| `plugins._base.db` | Plugin base database connection module |
| `auth-center.models` | Main DB reads (sms_templates migration source) |
| `auth-center.providers.sms.aliyun` | Aliyun SMS provider |
| `auth-center.providers.sms.twilio` | Twilio SMS provider |

### External Dependencies

| Dependency | Purpose |
|------------|---------|
| Aliyun SMS Service (Dysmsapi) | Domestic SMS sending |
| Twilio API | International SMS sending |

### Hooks Provided

| Hook Identifier | Description |
|-----------------|-------------|
| `sms/send` | Send an SMS verification code |
| `sms/generate_code` | Generate a random verification code |
| `sms/validate_phone` | Validate phone number format |
| `sms/check_rate_limit` | Check send rate limit |

### Dynamic Authentication Methods

| Method | Description |
|--------|-------------|
| `get_login_methods` | Provides SMS verification code login |
| `get_register_methods` | Provides SMS verification code registration |

## Menu Group

- **Security & Compliance** - SMS Management

## License

This plugin is part of the VeroRun platform and follows the platform's unified license agreement.
