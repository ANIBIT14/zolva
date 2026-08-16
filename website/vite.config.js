import { resolve } from "node:path";
import { defineConfig } from "vite";

// Multi-page app: /docs/ is a real file on disk, so a hard reload on any
// route always resolves — no SPA fallback needed on Cloudflare.
export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, "index.html"),
        docs: resolve(import.meta.dirname, "docs/index.html"),
        "docs-dashboard": resolve(import.meta.dirname, "docs/dashboard/index.html"),
        demo: resolve(import.meta.dirname, "demo/index.html"),
        playbooks: resolve(import.meta.dirname, "playbooks/index.html"),
        "playbook-voice": resolve(import.meta.dirname, "playbooks/voice-cx-elevenlabs/index.html"),
        "playbook-whatsapp": resolve(import.meta.dirname, "playbooks/whatsapp-collections/index.html"),
        "playbook-ci": resolve(import.meta.dirname, "playbooks/ci-gated-releases/index.html"),
        "playbook-slack": resolve(import.meta.dirname, "playbooks/slack-handover-desk/index.html"),
        "playbook-sms": resolve(import.meta.dirname, "playbooks/sms-collections-twilio-razorpay/index.html"),
        "playbook-telegram": resolve(import.meta.dirname, "playbooks/telegram-support-zendesk/index.html"),
        "playbook-redteam": resolve(import.meta.dirname, "playbooks/red-team-synthetics/index.html"),
        "playbook-feedback": resolve(import.meta.dirname, "playbooks/feedback-to-fix-loop/index.html"),
        "playbook-audit": resolve(import.meta.dirname, "playbooks/regulator-ready-audit/index.html"),
        "playbook-gateway": resolve(import.meta.dirname, "playbooks/self-hosted-llm-gateway/index.html"),
        "playbook-rcs": resolve(import.meta.dirname, "playbooks/rcs-fraud-alerts/index.html"),
        "playbook-contactcaps": resolve(import.meta.dirname, "playbooks/cross-channel-contact-caps/index.html"),
        "playbook-redaction": resolve(import.meta.dirname, "playbooks/pii-redaction-data-residency/index.html"),
        "use-cases": resolve(import.meta.dirname, "use-cases/index.html"),
        "uc-support": resolve(import.meta.dirname, "use-cases/banking-customer-support/index.html"),
        "uc-collections": resolve(import.meta.dirname, "use-cases/loan-collections/index.html"),
        "uc-fraud": resolve(import.meta.dirname, "use-cases/fraud-and-disputes/index.html"),
        "uc-kyc": resolve(import.meta.dirname, "use-cases/kyc-onboarding/index.html"),
        "uc-insurance": resolve(import.meta.dirname, "use-cases/insurance-claims/index.html"),
        "uc-healthcare": resolve(import.meta.dirname, "use-cases/healthcare-patient-support/index.html"),
        "uc-telecom": resolve(import.meta.dirname, "use-cases/telecom-customer-operations/index.html"),
        blog: resolve(import.meta.dirname, "blog/index.html"),
        "post-aiact": resolve(import.meta.dirname, "blog/eu-ai-act-ai-agents-financial-services/index.html"),
        "post-dora": resolve(import.meta.dirname, "blog/dora-llm-vendor-concentration-risk/index.html"),
        "post-guardrails": resolve(import.meta.dirname, "blog/guardrails-are-config-not-prompts/index.html"),
        "post-audit": resolve(import.meta.dirname, "blog/what-a-defensible-ai-audit-trail-contains/index.html"),
        "post-frameworks": resolve(import.meta.dirname, "blog/choosing-an-agent-framework-for-regulated-industries/index.html"),
        "post-rbi": resolve(import.meta.dirname, "blog/rbi-free-ai-framework-what-to-build/index.html"),
        "post-injection": resolve(import.meta.dirname, "blog/prompt-injection-owasp-agentic-2026/index.html"),
        "post-mcp": resolve(import.meta.dirname, "blog/mcp-in-a-bank-tool-access-without-blast-radius/index.html"),
        "post-voice-compliance": resolve(import.meta.dirname, "blog/ai-voice-agent-disclosure-collections/index.html"),
        security: resolve(import.meta.dirname, "security/index.html"),
        contributing: resolve(import.meta.dirname, "contributing/index.html"),
        legal: resolve(import.meta.dirname, "legal/index.html"),
        404: resolve(import.meta.dirname, "404.html"),
      },
    },
  },
});
