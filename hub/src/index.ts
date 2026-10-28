import { createPool, poolQueryable } from "./db.js";
import { loadConfig } from "./config.js";
import { createApp } from "./createApp.js";

const config = loadConfig();
const pool = createPool(config.databaseUrl);
const app = createApp({ config, db: poolQueryable(pool) });

const port = Number(process.env.PORT ?? "8787");
app.listen(port, () => {
  console.log(`airlie-hub listening on :${port}`);
});
