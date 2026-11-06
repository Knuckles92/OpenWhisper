mod files;
mod meeting_tools;
mod meetings;
mod walkthrough;

pub use files::files;
pub use meeting_tools::{meeting_tools_demo, meeting_tools_demo_command, meeting_tools_response};
pub use meetings::meetings;
pub use walkthrough::{
    walkthrough, walkthrough_block_navigation, walkthrough_event, walkthrough_response,
};

pub(super) fn reload_walkthrough_gallery(s: &mut crate::state::State) {
    walkthrough::reload_gallery(s);
}
