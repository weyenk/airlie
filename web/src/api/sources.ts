// A person's repositories from linked integrations (`GET /v1/airlie/chat/repositories`) and GitHub's device
// flow to link one (`POST …/sources/{source}/link`, poll, unlink).

import { request } from "./index";

export type SourceRepo = { repo: string; private: boolean; push: boolean; url: string };

export type SourceView = {
  name: string;
  title: string;
  linked: boolean;
  login: string | null;
  repositories: SourceRepo[];
  error: string | null;
  listed_at: number | null;
  status?: string;
  source?: string;
  fix?: string;
};

export type PersonRepos = { sources: SourceView[]; extras: string[]; extra_candidates: string[] };

export type LinkStart = {
  link_id: string;
  user_code: string;
  verification_uri: string;
  expires_at: number;
  interval: number;
};

export type LinkPoll = { state: "linked"; login: string } | { state: "pending"; interval: number };

const REPOS = "/v1/airlie/chat/repositories";

export function personRepositories(refresh = false): Promise<PersonRepos> {
  return request<PersonRepos>(`${REPOS}${refresh ? "?refresh=true" : "?refresh=false"}`);
}

export function startLink(source: string): Promise<LinkStart> {
  return request<LinkStart>(`/v1/airlie/chat/sources/${encodeURIComponent(source)}/link`, { method: "POST" });
}

export function pollLink(source: string, linkId: string): Promise<LinkPoll> {
  return request<LinkPoll>(`/v1/airlie/chat/sources/${encodeURIComponent(source)}/link/${encodeURIComponent(linkId)}`, {
    method: "POST",
  });
}

export function unlink(source: string): Promise<void> {
  return request<void>(`/v1/airlie/chat/sources/${encodeURIComponent(source)}/link`, { method: "DELETE" });
}
