# Order Notifications (order_notify)

## Overview

Order Notifications is VeroRun's event-driven automatic order notification plugin. It listens to key order lifecycle events (placed, paid, shipped, refunded, cancelled, completed) and automatically triggers the corresponding notification logic, keeping users and admins informed of order status changes.

## Features

- **Event-driven Architecture**: Listens to 6 order lifecycle events via the VeroRun event system
- **Full Lifecycle Coverage**: Covers the complete order flow from placement to completion
- **Automatic Notification**: Notification logic runs automatically on event trigger; no manual calls needed
- **Dedicated Database**: Stores notification records in an independent SQLite database
- **Lightweight Design**: Minimal module footprint (data model + event listeners)

## Architecture

```
VeroRun Event Bus
   ├── order/created    (placed)
   ├── order/paid       (paid)
   ├── order/shipped    (shipped)
   ├── order/refunded   (refunded)
   ├── order/cancelled  (cancelled)
   └── order/completed  (completed)
            ↓
   __init__.py (listener registration + notification dispatch)
            ↓
   models.py (notification records / data/order_notify.db)
```

| Event | Trigger | Notification Behavior |
|------|----------|----------|
| `order/created` | User places an order | Order placed notification |
| `order/paid` | Payment succeeds | Payment success notification |
| `order/shipped` | Merchant ships | Shipping notification (with tracking info) |
| `order/refunded` | Refund completes | Refund notification |
| `order/cancelled` | Order cancelled | Cancellation notification |
| `order/completed` | Order completes | Completion notification |

## Installation & Enablement

### Installation

The plugin ships with VeroRun's default plugin directory; no separate installation step is required.

### Enablement

1. Enable the Order Notifications plugin on the VeroRun admin "Plugin Management" page
2. The 6 event listeners register automatically on the event bus
3. Notification logic runs automatically when order status events fire

## Configuration

| Config Key | Description | Default |
|--------|------|--------|
| `database.type` | Database type | `sqlite` |
| `database.path` | Database file path | `data/order_notify.db` |
| `notifications.enabled_events` | Enabled notification events | all 6 events |
| `notifications.channels` | Notification channels | `email, site_message` |

## API Endpoints

### Listened Hooks

| Hook Identifier | Description |
|-------------|------|
| `order/created` | Order placed notification |
| `order/paid` | Payment success notification |
| `order/shipped` | Shipping notification |
| `order/refunded` | Refund notification |
| `order/cancelled` | Cancellation notification |
| `order/completed` | Completion notification |

### Admin Panel

This plugin has no admin menu.

## Dependencies

### Internal Dependencies

- VeroRun core framework: event bus, Hook system
- **email** plugin: email notification channel
- Order system: produces order lifecycle events

### Dependents

- Order system: order events drive this plugin
- Frontend user center: displays order notification records

## License

This plugin is part of the VeroRun project and follows the overall license of the VeroRun project.
