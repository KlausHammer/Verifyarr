export interface AuthStatus {
  needs_setup: boolean
  authenticated: boolean
  username: string | null
}

export interface FileRow {
  id: number
  subtitle_path: string | null
  video_path: string
  lang: string | null
  media_root: string | null
  season_episode: string | null
  series_or_movie_title: string | null
  video_mtime: number | null
  video_size: number | null
  subtitle_mtime: number | null
  subtitle_size: number | null
  last_processed: string | null
  sync_status: string | null
  sync_max_shift_s: number | null
  structural_change: number
  sync_split_blocks: number | null
  correctness_flag: string | null
  correctness_avg_score: number | null
  line_order_fixed: number | null
  line_order_flagged: number | null
  note: string | null
  auto_action: string | null
  last_run_id: number | null
}

export interface CorrectnessHistoryRow {
  id: number
  subtitle_path: string
  video_path: string | null
  lang: string | null
  checked_at: string
  correctness_flag: string | null
  correctness_avg_score: number | null
  audio_lang: string | null
  samples_json: string | null
  run_id: number | null
}

export interface Paginated<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface RunRow {
  id: number
  trigger: string
  mode: string
  status: 'running' | 'completed' | 'cancelled' | 'failed'
  started_at: string
  finished_at: string | null
  files_total: number | null
  files_processed: number
  files_changed: number
  files_suspect: number
  files_error: number
  dry_run: number
  force: number
  error_message: string | null
  target_kind: string | null
  target_title: string | null
}

export interface LogLine {
  id: number
  run_id: number
  ts: string
  level: string
  message: string
}

export interface AppLogLine {
  id: number
  ts: string
  level: string
  message: string
}

export interface StatsSummary {
  files: {
    total: number
    missing: number
    suspect: number
    ok: number
    errors: number
    out_of_sync: number
    line_order_fixed_total: number
    line_order_flagged_total: number
  }
  by_lang: { lang: string; avg_score: number | null; n: number }[]
  by_kind: { kind: string; n: number; suspect: number; missing: number }[]
  score_distribution: { bucket: string; n: number }[]
  last_run: RunRow | null
}

export interface MatchRatePoint {
  period: string
  total: number
  ok_count: number
  suspect_count: number
  avg_score: number | null
}

export interface SeasonEntry {
  season: string
  video_count: number
  subtitle_detected_count: number
  processed_count: number
  ok_count: number
  suspect_count: number
  missing_count: number
  last_processed: string | null
}

export interface LibraryEntry {
  title: string
  video_count: number
  subtitle_detected_count: number
  processed_count: number
  ok_count: number
  suspect_count: number
  missing_count: number
  last_processed: string | null
  // True as soon as ANY video under this title matched something in Bazarr -- False means the
  // title shown is a folder/filename guess, not a Bazarr-confirmed name (see the "not in
  // Bazarr" badge in MediaLibrary.tsx).
  bazarr_matched: boolean
  seasons: SeasonEntry[] | null
}

export interface LibraryResponse {
  items: LibraryEntry[]
  total: number
  last_scanned_at: string | null
  // Only present on the POST /library/rescan response ("Detect now" button) -- not on a plain GET.
  pairs_found?: number
  missing_found?: number
  cancelled?: boolean
}

export interface ArchivedItem {
  path: string
  original_name: string
  size: number
  mtime: number
}

export interface BlacklistAction {
  id: number
  subtitle_path: string | null
  video_path: string | null
  kind: string | null
  provider: string | null
  subs_id: string | null
  language: string | null
  series_id: string | null
  episode_id: string | null
  radarr_id: string | null
  blacklisted_at: string
  run_id: number | null
  remediation_outcome: string | null
}

export interface SecretField {
  is_set: boolean
}

export interface GeneralSettings {
  movies_folder: string
  series_folder: string
  subtitle_langs: string[]
  backup_originals: boolean
  auto_scan_sync_enabled: boolean
  auto_scan_correctness_enabled: boolean
  auto_scan_line_order_enabled: boolean
}

export interface SyncSettings {
  enabled: boolean
  split_penalty: number
  min_change_seconds: number
  sample_count: number
  clip_seconds: number
  window_minutes: number
  overlap_threshold: number
  line_order_enabled: boolean
  line_order_audio_confirm: boolean
  line_order_swap_threshold_pct: number
  line_order_swap_threshold_min: number
  line_order_auto_action: 'off' | 'quarantine' | 'blacklist' | 'remediate'
}

export interface CorrectnessSettings {
  enabled: boolean
  stt_provider: 'groq' | 'openrouter'
  groq_api_key: SecretField
  groq_model: string
  groq_model_fallback: string
  groq_llm_model: string
  groq_llm_model_fallback: string
  openrouter_api_key: SecretField
  openrouter_stt_model: string
  openrouter_stt_model_fallback: string
  openrouter_llm_model: string
  openrouter_llm_model_fallback: string
  require_audio_lang: string
  auto_action: 'off' | 'quarantine' | 'blacklist' | 'remediate'
}

export interface AutomationSettings {
  remediate_max_attempts: number
  remediate_min_score: number
  dry_run: boolean
}

export interface BazarrSettings {
  url: string
  api_key: SecretField
  path_map: [string, string][]
}

export interface SchedulingSettings {
  cron: string
  run_on_start: boolean
  poll_new_media_enabled: boolean
  poll_new_media_interval_minutes: number
  poll_library_enabled: boolean
  poll_library_interval_minutes: number
}

export interface LogSettings {
  level: string
}

export interface AllSettings {
  general: GeneralSettings
  sync: SyncSettings
  correctness: CorrectnessSettings
  automation: AutomationSettings
  bazarr: BazarrSettings
  scheduling: SchedulingSettings
  log: LogSettings
}
