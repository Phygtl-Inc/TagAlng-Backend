# Guest onboarding — in-chat flow (Lana Phase 1)

All of this happens **inside the Lana chat**. The frontend reads `onboarding_step` and `requires_phone_verification` on each `/lana/sessions/{id}/messages` response.

## Example conversation

| Turn | Who | Message |
|------|-----|---------|
| 0 | Lana | So — who are you, right now? Tell me your life stage and what you're hoping to find on the block. |
| 1 | User | I'm a Latino mom in Lake Nona, new here about 3 months. |
| 2 | Lana | Maria told me two hours ago she's looking for Brazilian moms too. Want me to put you two together? |
| 3 | User | Yes |
| 4 | Lana | Love it! What should Maria call you when I introduce you? |
| 5 | User | Linda |
| 6 | Lana | Perfect, Linda! Before I introduce you to Maria, verify your phone — use the button below. |
| — | **FE** | `updateUser({ phone })` then OTP UI (`requires_phone_verification: true`) |
| — | **API** | Verify with `type: phone_change` (NOT `sms` — sms creates a different user) |
| 7 | User | *(after OTP)* ok |
| 8 | Lana | You're verified! Quick one — how many kids do you have, and what are their ages? |
| 9 | User | Two kids, 5 and 8 |
| 10 | Lana | Great — what kind of get-togethers are you hoping for on the block? |
| 11 | User | Weekend activities with other moms |
| 12 | Lana | Love that. Tap Complete when you're ready! |
| — | **FE** | `assign_home_block` → `send_joint_moment_intro` |

## API response fields (each message)

```json
{
  "assistant_message": "...",
  "onboarding_step": "offered_intro",
  "requires_phone_verification": false,
  "joint_moment": {
    "joint_moment_id": "uuid",
    "candidate": { "nickname": "Maria", "user_id": "..." },
    "lana_copy": "...",
    "is_demo": true
  },
  "phone_verified": false,
  "home_block_assigned": false
}
```

### `onboarding_step` values

| Step | Meaning | FE action |
|------|---------|-----------|
| `early_chat` | Collecting life stage / heritage | Chat only |
| `offered_intro` | Waiting for yes/no to Maria intro | Chat; optional Yes/No buttons |
| `awaiting_intro_name` | Need name for intro | Chat |
| `await_phone` | Need OTP | Show phone verification UI |
| `post_verify` | Phone done; finish profile | Chat → Complete |
| `intro_declined` | User said no to intro | Normal profile chat |

## After Complete

1. `POST /rest/v1/rpc/assign_home_block` (same block as Maria for demo)
2. `POST /rest/v1/rpc/send_joint_moment_intro` with `joint_moment_id` from session

Postman: `docs/postman/TagAlng-Guest-Onboarding-Full.postman_collection.json`
