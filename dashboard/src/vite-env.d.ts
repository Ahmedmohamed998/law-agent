/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Where the product backend lives. Set it in `dashboard/.env` — the default
   * is localhost, which is only ever right on the machine running the service.
   */
  readonly VITE_BACKEND_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
