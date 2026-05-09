export interface ProjectProfileUpsert {
    session_id: string;
    project_id: string;
    project_name: string;
    api_base_url?: string;
    aws_s3_bucket?: string;
    firestore_root?: string;
    enabled_tools: string[];
    api_key?: string;
    custom_config: Record<string, any>;
}

export interface ProjectProfileResponse {
    project_profile: ProjectProfileUpsert | null;
}
