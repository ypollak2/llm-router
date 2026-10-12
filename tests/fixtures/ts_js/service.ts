import { readFile } from "fs/promises";
import path, { join as joinPath } from "path";
import type { Config } from "./config";
import "./side-effect";
// function commentedOut(a) { }
/* class CommentedClass { } */
const note = "function inString(x) { }";

export interface Options {
  retries: number;
}

export type Handler = (req: Request) => Promise<Response>;

export enum Mode { Fast, Slow }

export async function loadConfig(file: string): Promise<Config> {
  const text = await readFile(joinPath("/etc", file), "utf8");
  return JSON.parse(text);
}

function helper(a: number, b: number) {
  if (a > b) {
    return { a };
  }
  return b;
}

export class Router {
  private routes: string[] = [];
  add(route: string) { this.routes.push(route); }
}

export const makeId = (prefix: string): string => `${prefix}-${Date.now()}`;
const pick = async x => x;
const legacy = function (a) { return a; };

export default class DefaultThing {}

export { helper as publicHelper, pick };
