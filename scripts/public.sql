-- WARNING: This schema is for context only and is not meant to be run.
-- Table order and constraints may not be valid for execution.

CREATE TABLE public.workspaces (
  id text NOT NULL,
  name text NOT NULL,
  owner_id uuid,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  show_attribution boolean NOT NULL DEFAULT true,
  stage text NOT NULL DEFAULT 'note'::text CHECK (stage = ANY (ARRAY['note'::text, 'research'::text, 'plan'::text, 'build'::text, 'test'::text, 'growth'::text])),
  stage_history jsonb NOT NULL DEFAULT '[]'::jsonb,
  active_stages jsonb NOT NULL DEFAULT '["note"]'::jsonb,
  CONSTRAINT workspaces_pkey PRIMARY KEY (id),
  CONSTRAINT workspaces_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES auth.users(id)
);
CREATE TABLE public.workspace_members (
  workspace_id text NOT NULL,
  user_id uuid NOT NULL,
  added_at timestamp with time zone NOT NULL DEFAULT now(),
  role text NOT NULL DEFAULT 'viewer'::text CHECK (role = ANY (ARRAY['viewer'::text, 'editor'::text, 'moderator'::text, 'partner'::text])),
  can_toggle_attribution boolean NOT NULL DEFAULT false,
  CONSTRAINT workspace_members_pkey PRIMARY KEY (workspace_id, user_id),
  CONSTRAINT workspace_members_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id),
  CONSTRAINT workspace_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id)
);
CREATE TABLE public.chats (
  id text NOT NULL,
  title text NOT NULL DEFAULT 'New Chat'::text,
  owner_id uuid NOT NULL,
  workspace_id text,
  is_private boolean NOT NULL DEFAULT false,
  tags ARRAY NOT NULL DEFAULT '{}'::text[],
  template_id text,
  linked_chat_ids ARRAY NOT NULL DEFAULT '{}'::text[],
  messages jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT chats_pkey PRIMARY KEY (id),
  CONSTRAINT chats_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES auth.users(id),
  CONSTRAINT chats_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id)
);
CREATE TABLE public.batches (
  id text NOT NULL,
  name text NOT NULL,
  owner_id uuid NOT NULL,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT batches_pkey PRIMARY KEY (id),
  CONSTRAINT batches_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES auth.users(id)
);
CREATE TABLE public.batch_members (
  batch_id text NOT NULL,
  chat_id text NOT NULL,
  CONSTRAINT batch_members_pkey PRIMARY KEY (batch_id, chat_id),
  CONSTRAINT batch_members_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.batches(id),
  CONSTRAINT batch_members_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id)
);
CREATE TABLE public.workspace_owner_votes (
  workspace_id text NOT NULL,
  voter_id uuid NOT NULL,
  vote_target uuid,
  cast_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT workspace_owner_votes_pkey PRIMARY KEY (workspace_id, voter_id),
  CONSTRAINT workspace_owner_votes_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id)
);
CREATE TABLE public.audit_log (
  id bigint NOT NULL DEFAULT nextval('audit_log_id_seq'::regclass),
  user_id uuid NOT NULL,
  action text NOT NULL,
  target_type text NOT NULL,
  target_id text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT audit_log_pkey PRIMARY KEY (id)
);
CREATE TABLE public.user_integrations (
  id bigint NOT NULL DEFAULT nextval('user_integrations_id_seq'::regclass),
  user_id uuid NOT NULL,
  provider text NOT NULL,
  account_label text,
  access_token text NOT NULL,
  refresh_token text,
  scope text,
  expires_at timestamp with time zone,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT user_integrations_pkey PRIMARY KEY (id)
);
CREATE TABLE public.workspace_panel_content (
  workspace_id text NOT NULL,
  panel_key text NOT NULL,
  content text NOT NULL DEFAULT ''::text,
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  updated_by text,
  source_node_ids ARRAY,
  content_source text NOT NULL DEFAULT 'manual'::text CHECK (content_source = ANY (ARRAY['manual'::text, 'chat'::text])),
  CONSTRAINT workspace_panel_content_pkey PRIMARY KEY (workspace_id, panel_key),
  CONSTRAINT workspace_panel_content_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id)
);
CREATE TABLE public.chat_messages (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  chat_id text NOT NULL,
  owner_id text NOT NULL,
  seq bigint NOT NULL,
  role text,
  payload jsonb NOT NULL,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT chat_messages_pkey PRIMARY KEY (id),
  CONSTRAINT chat_messages_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id)
);
CREATE TABLE public.workspace_code_files (
  workspace_id text NOT NULL,
  file_path text NOT NULL,
  content text NOT NULL DEFAULT ''::text,
  language text,
  updated_at timestamp with time zone,
  updated_by text,
  version integer NOT NULL DEFAULT 1,
  CONSTRAINT workspace_code_files_pkey PRIMARY KEY (workspace_id, file_path),
  CONSTRAINT workspace_code_files_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id)
);
CREATE TABLE public.llm_call_log (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  session_id text,
  agent_name text NOT NULL DEFAULT 'Agent'::text,
  provider text NOT NULL,
  model text,
  key_id text,
  tier smallint,
  path text,
  domain text,
  input_tokens integer,
  output_tokens integer,
  total_tokens integer,
  cost_usd numeric,
  latency_ms integer,
  finish_reason text,
  error text,
  CONSTRAINT llm_call_log_pkey PRIMARY KEY (id)
);
CREATE TABLE public.workspace_code_file_versions (
  workspace_id text NOT NULL,
  file_path text NOT NULL,
  version integer NOT NULL,
  content text NOT NULL DEFAULT ''::text,
  updated_at timestamp with time zone,
  updated_by text,
  source text NOT NULL DEFAULT 'user'::text CHECK (source = ANY (ARRAY['user'::text, 'pipeline'::text, 'proposal'::text, 'restore'::text])),
  CONSTRAINT workspace_code_file_versions_pkey PRIMARY KEY (workspace_id, file_path, version),
  CONSTRAINT workspace_code_file_versions_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id)
);
CREATE TABLE public.workspace_code_proposals (
  id text NOT NULL,
  workspace_id text NOT NULL,
  session_id text,
  created_by text NOT NULL,
  instruction text NOT NULL,
  status text NOT NULL DEFAULT 'pending'::text CHECK (status = ANY (ARRAY['pending'::text, 'accepted'::text, 'rejected'::text, 'partial'::text, 'stale'::text, 'failed'::text])),
  files jsonb NOT NULL DEFAULT '[]'::jsonb,
  summary text,
  refs jsonb NOT NULL DEFAULT '[]'::jsonb,
  model_meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  resolved_at timestamp with time zone,
  CONSTRAINT workspace_code_proposals_pkey PRIMARY KEY (id),
  CONSTRAINT workspace_code_proposals_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id)
);
CREATE TABLE public.workspace_code_findings (
  workspace_id text NOT NULL,
  file_path text NOT NULL,
  seq integer NOT NULL CHECK (seq >= 0),
  line integer CHECK (line IS NULL OR line >= 1),
  severity text NOT NULL CHECK (severity = ANY (ARRAY['error'::text, 'warning'::text, 'info'::text])),
  message text NOT NULL,
  source text NOT NULL,
  file_version integer,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT workspace_code_findings_pkey PRIMARY KEY (workspace_id, file_path, seq),
  CONSTRAINT workspace_code_findings_workspace_id_file_path_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspace_code_files(workspace_id),
  CONSTRAINT workspace_code_findings_workspace_id_file_path_fkey FOREIGN KEY (file_path) REFERENCES public.workspace_code_files(file_path)
);