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
        security: resolve(import.meta.dirname, "security/index.html"),
        contributing: resolve(import.meta.dirname, "contributing/index.html"),
        legal: resolve(import.meta.dirname, "legal/index.html"),
        404: resolve(import.meta.dirname, "404.html"),
      },
    },
  },
});
