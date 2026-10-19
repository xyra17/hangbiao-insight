import { readFileSync } from "node:fs";
import { resolve } from "node:path";

export type AcceptanceAccount = { username: string; password: string };

const accountFile = process.env.ACCEPTANCE_ACCOUNTS_FILE;
if (!accountFile) {
  throw new Error("Set ACCEPTANCE_ACCOUNTS_FILE to a private local acceptance account file.");
}

export const accounts = JSON.parse(readFileSync(resolve(accountFile), "utf8")) as AcceptanceAccount[];
