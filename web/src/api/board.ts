// The board's routes (`/v1/board`), typed from what the server returns (airlie.board.model.Item.to_dict).

import { queryString, request } from "./index";

export type Level = "initiative" | "epic" | "story" | "subtask";
export type Status = "todo" | "approved" | "doing" | "review" | "blocked" | "done";

export type Comment = { id: number; author: string; text: string; at: number };

export type Item = {
  key: string;
  level: Level;
  title: string;
  /** Markdown. */
  description: string;
  status: Status;
  parent: string | null;
  /** A `repo:<owner>/<name>` label names a story's repository. */
  labels: string[];
  comments: Comment[];
  created_at: number;
  updated_at: number;
  created_by: string;
};

export type NewItem = { level: Level; title: string; description?: string; parent?: string | null; labels?: string[] };
export type ItemChange = {
  title?: string;
  description?: string;
  status?: Status;
  parent?: string | null;
  labels?: string[];
};

const itemPath = (key: string) => `/v1/board/items/${encodeURIComponent(key)}`;

export async function listItems(filter: { level?: Level; status?: Status; parent?: string } = {}): Promise<Item[]> {
  return (await request<{ items: Item[] }>(`/v1/board/items${queryString(filter)}`)).items;
}

export function getItem(key: string): Promise<Item> {
  return request<Item>(itemPath(key));
}

export function createItem(body: NewItem): Promise<Item> {
  return request<Item>("/v1/board/items", { method: "POST", body });
}

export function patchItem(key: string, body: ItemChange): Promise<Item> {
  return request<Item>(itemPath(key), { method: "PATCH", body });
}

/** Put the item in `status`, above the card `before` (none: at the bottom of the column). The server owns the
 * order, so the caller takes the list back whole afterwards. */
export function moveItem(key: string, status: Status, before: string | null = null): Promise<Item> {
  return request<Item>(`${itemPath(key)}/move`, { method: "POST", body: { status, before } });
}

export function addComment(key: string, text: string): Promise<Item> {
  return request<Item>(`${itemPath(key)}/comments`, { method: "POST", body: { text } });
}

export function editComment(key: string, id: number, text: string): Promise<Item> {
  return request<Item>(`${itemPath(key)}/comments/${id}`, { method: "PATCH", body: { text } });
}
