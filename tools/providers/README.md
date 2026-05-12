# Compliance providers — drop-in interfaces

Each module here exposes a stable Python API used by the zkCEX demo services
(auth_server, chain_server). They run as stubs by default; setting the right
env var swaps in a real provider without changing call sites.

## Providers

| Module           | Purpose                                  | Env to switch on real provider       | Demo behaviour                                                    |
|------------------|------------------------------------------|--------------------------------------|-------------------------------------------------------------------|
| aml_provider.py  | Sanctions / risk screening (zkAML)       | ZKAML_URL, ZKAML_API_KEY             | Hardcoded sanctions list + deterministic score from address bytes |
| sms_provider.py  | 6-digit OTP via Korean carrier           | SMS_PROVIDER_URL, SMS_PROVIDER_KEY   | stderr log, returns ok=True                                       |
| kyc_provider.py  | 본인인증 (NICE checkplus / KCB / KMC)     | KYC_PROVIDER_URL, KYC_PROVIDER_KEY   | format validation only                                            |
| kyc_provider.py  | Sumsub WebSDK (real document KYC)        | SUMSUB_APP_TOKEN, SUMSUB_APP_SECRET  | Endpoints respond 503; UI hides Sumsub tab and falls back to PASS |

When a real provider is available, the call site does NOT change. Only the
import returns a different decision. Failure-mode is fail-closed (REVIEW or
verified=False) so a provider outage does not silently let traffic through.
