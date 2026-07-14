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
        playbooks: resolve(import.meta.dirname, "playbooks/index.html"),
        "playbook-voice": resolve(import.meta.dirname, "playbooks/voice-cx-elevenlabs/index.html"),
        "playbook-whatsapp": resolve(import.meta.dirname, "playbooks/whatsapp-collections/index.html"),
        "playbook-ci": resolve(import.meta.dirname, "playbooks/ci-gated-releases/index.html"),
        security: resolve(import.meta.dirname, "security/index.html"),
        contributing: resolve(import.meta.dirname, "contributing/index.html"),
        404: resolve(import.meta.dirname, "404.html"),
      },
    },
  },
});
