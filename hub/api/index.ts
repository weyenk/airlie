import express from "express";
import { createPool, poolQueryable } from "../src/db.js";
import { loadConfig } from "../src/config.js";
import { createApp } from "../src/createApp.js";

const config = loadConfig();
const pool = createPool(config.databaseUrl);
const app = createApp({ config, db: poolQueryable(pool) });

export default app satisfies express.Express;
