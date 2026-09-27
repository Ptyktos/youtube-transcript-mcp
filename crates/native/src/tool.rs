// rmcp's `#[tool_handler]`/`#[tool_router]` macros parse their impl block with
// no tolerance for an adjacent attribute in either order -- `#[expect(...)]`
// directly on the impl produces a bogus "expected `fn`" error instead of
// suppressing the lint (confirmed empirically against this exact macro
// earlier in the rmcp 0.1.x version of this file). Module-scoped instead.
#![expect(clippy::unused_async_trait_impl)]

use reqwest::Client;
use rmcp::{
    handler::server::{tool::ToolRouter, wrapper::Parameters},
    model::{CallToolResult, ContentBlock, ServerCapabilities, ServerConfig},
    schemars, tool, tool_handler, tool_router, ErrorData as McpError, ServerHandler,
};
use serde::Deserialize;
use youtube_transcript_mcp_core::TranscriptError;

use crate::fetch::get_transcript;

#[derive(Debug, Deserialize, schemars::JsonSchema)]
pub struct GetTranscriptRequest {
    /// `YouTube` video URL (any format: watch, youtu.be, shorts, live, embed)
    pub url: String,
    /// Language code (e.g. 'en', 'es', 'fr'). Omit or use 'auto' for automatic detection.
    pub language: Option<String>,
    /// Output format: 'text' (default), 'json', 'srt', 'vtt', or 'markdown'. 'json' and
    /// 'markdown' embed clickable links to each timestamp in the video.
    pub format: Option<String>,
}

#[derive(Clone)]
pub struct TranscriptServer {
    client: Client,
    #[expect(dead_code, reason = "tool_handler macro accesses this router field")]
    tool_router: ToolRouter<Self>,
}

impl TranscriptServer {
    #[must_use]
    pub fn new(client: Client) -> Self {
        Self {
            client,
            tool_router: Self::tool_router(),
        }
    }
}

#[tool_router]
impl TranscriptServer {
    /// Extract the full transcript from a `YouTube` video.
    #[tool(
        description = "Extract the transcript from a YouTube video URL. The 'format' argument selects plain text (default), JSON or Markdown (both with clickable timestamp links), or SRT/VTT subtitles."
    )]
    async fn get_transcript(
        &self,
        request: Parameters<GetTranscriptRequest>,
    ) -> Result<CallToolResult, McpError> {
        let Parameters(GetTranscriptRequest {
            url,
            language,
            format,
        }) = request;
        let lang = language.as_deref().unwrap_or("auto");
        let fmt = format.as_deref().unwrap_or("text");
        get_transcript(&self.client, &url, lang, fmt)
            .await
            .map(|r| CallToolResult::success(vec![ContentBlock::text(r.text)]))
            .map_err(|e| match &e {
                TranscriptError::InvalidUrl | TranscriptError::InvalidVideoId => {
                    McpError::invalid_params(e.to_string(), None)
                }
                _ => McpError::internal_error(e.to_string(), None),
            })
    }
}

#[tool_handler]
impl ServerHandler for TranscriptServer {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build()).with_server_info(
            rmcp::model::Implementation::new("youtube-transcript-mcp", env!("CARGO_PKG_VERSION")),
        )
    }
}
