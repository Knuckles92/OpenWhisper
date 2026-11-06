//! Native crop and annotation editor. All geometry sent to the service uses original image pixels.
mod drawing;
mod model;
#[cfg(test)]
mod tests;

use crate::state::State;
use crate::ui::{Palette, btn, card, label, link};
use model::{
    SWATCHES, TOOLS, choose_tool, choose_window, delete_selected, dimensions, n, remember, text,
    undo, valid_region,
};
pub use model::{begin_screenshot, finish_screenshot, screenshot_finished, screenshot_key};
use rune::{Cursor, El, SurfaceFit, col, el, row};
use serde_json::json;

pub fn screenshot(s: &State, p: &Palette) -> El {
    let request = &s.data["screenshot.choose"];
    let e = &s.data["screenshot.editor"];
    let tool = text(e, "tool", "select");
    let [w, h] = dimensions(s);
    let mut tools = row().gap(5.).flex_wrap();
    for &(id, title) in TOOLS {
        let enabled = id != "window"
            || request["windows"]
                .as_array()
                .is_some_and(|windows| !windows.is_empty());
        tools = tools.child(
            btn(
                format!("screenshot-tool-{id}"),
                title,
                p,
                tool == id,
                enabled && !s.pending,
                move |s, _| choose_tool(s, id),
            )
            .h(31.),
        );
    }
    let mut colors = row()
        .gap(5.)
        .center_y()
        .flex_wrap()
        .child(label("Ink", 10.5, 500., p.muted));
    for &(name, hex) in SWATCHES {
        let selected = e["color"] == hex;
        let color = rune::color::hex(hex).unwrap_or(p.accent);
        colors = colors.child(
            btn(
                format!("screenshot-color-{name}"),
                name,
                p,
                false,
                !s.pending,
                move |s, _| s.data["screenshot.editor"]["color"] = json!(hex),
            )
            .h(29.)
            .border(
                if selected { 2. } else { 1. },
                color.alpha(if selected { 1. } else { 0.45 }),
            )
            .fill(if selected {
                color.alpha(0.13)
            } else {
                p.surface
            }),
        );
    }
    for (width, title) in [(2., "Thin"), (4., "Normal"), (7., "Bold")] {
        colors = colors.child(
            btn(
                format!("screenshot-width-{title}"),
                title,
                p,
                n(e, "width", 4.) == width,
                !s.pending,
                move |s, _| s.data["screenshot.editor"]["width"] = json!(width),
            )
            .h(29.),
        );
    }
    let viewport = [
        e["viewport"][0].as_f64().unwrap_or(900.) as f32,
        e["viewport"][1].as_f64().unwrap_or(410.) as f32,
    ];
    let zoom = n(e, "zoom", 1.);
    let pan = [
        e["pan"][0].as_f64().unwrap_or(0.) as f32,
        e["pan"][1].as_f64().unwrap_or(0.) as f32,
    ];
    let path = text(request, "image_path", "");
    let mut canvas = el()
        .key("screenshot-canvas")
        .w_full()
        .h(410.)
        .fill(p.soft)
        .scale(zoom)
        .offset(pan[0], pan[1])
        .role("canvas")
        .label("Screenshot crop and annotation canvas")
        .cursor(match tool {
            "pan" => {
                if e["dragging"] == true {
                    Cursor::Grabbing
                } else {
                    Cursor::Grab
                }
            }
            "move" => Cursor::Move,
            "text" => Cursor::Text,
            "window" => Cursor::Pointer,
            _ => Cursor::Crosshair,
        })
        .on_layout(|s: &mut State, rect| {
            s.data["screenshot.editor"]["viewport"] = json!([rect[2], rect[3]])
        })
        .on_drag(|s: &mut State, d, _| model::drag(s, d))
        .on_pointer_move_local(|s: &mut State, event, _| {
            let e = &s.data["screenshot.editor"];
            let viewport = [
                e["viewport"][0].as_f64().unwrap_or(900.) as f32,
                e["viewport"][1].as_f64().unwrap_or(410.) as f32,
            ];
            model::hover(s, event.x, event.y, viewport);
        });
    let mut image_ready = false;
    if let Some(image) = s.images.get(path) {
        image_ready = image.is_ready() && image.error().is_none();
        canvas = canvas.pixels_fit(image.pixels(), SurfaceFit::Fit);
        if let Some(error) = image.error() {
            canvas = canvas.child(
                label(
                    format!("Could not display the capture: {error}"),
                    13.,
                    400.,
                    p.danger,
                )
                .padding(18.)
                .inert(),
            );
        } else if !image.is_ready() {
            canvas = canvas.child(
                label("Loading the capture…", 13., 400., p.muted)
                    .padding(18.)
                    .inert(),
            );
        }
    } else {
        canvas = canvas.child(
            label("Loading the capture…", 13., 400., p.muted)
                .padding(18.)
                .inert(),
        );
    }
    canvas = canvas.child(drawing::overlay(s, p, viewport));
    if s.pending || !image_ready {
        canvas = canvas.inert();
    }
    let region = &e["region"];
    let selected = valid_region(region);
    let undo_ready = e["undo"].as_array().is_some_and(|items| !items.is_empty());
    let redo_ready = e["redo"].as_array().is_some_and(|items| !items.is_empty());
    let mut content=col().key("screenshot-editor").gap(13.)
        .child(row().gap(12.).center_y().flex_wrap()
            .child(col().grow(1.).min_w(0.).gap(5.)
                .child(label("Keep the detail. Add the context.",23.,500.,p.ink))
                .child(label(if tool=="window"{"Click a window in the capture, or choose one below."}else if tool=="select"{"Drag across the capture to select the region you want to keep."}else if tool=="move"{"Drag an annotation to reposition it. Delete removes the selected mark."}else{"Draw on the capture. Your crop and annotations are saved together."},11.5,400.,p.muted)))
            .child(btn("screenshot-cancel","Cancel",p,false,!s.pending,|s,_|finish_screenshot(s,true)))
            .child(btn("screenshot-keep",if s.pending{"Keeping…"}else{"Keep screenshot"},p,true,selected && image_ready && !s.pending,|s,_|finish_screenshot(s,false))))
        .child(tools).child(colors);
    if tool == "text" {
        content = content.child(
            super::library::field(
                s,
                p,
                "screenshot-label",
                "Type a label, then click where it belongs",
                false,
            )
            .on_submit(|s: &mut State, _| s.edit("screenshot-label").focused = false),
        );
    }
    if request["mode"] == "window"
        && request["windows"]
            .as_array()
            .is_none_or(|windows| windows.is_empty())
    {
        content=content.child(label("This desktop did not provide window bounds. Draw a region to choose exactly what to keep.",11.5,400.,p.muted));
    }
    content=content.child(el().w_full().h(410.).radius(10.).clip_rounded(10.).border(1.,p.border).child(canvas))
        .child(row().gap(7.).center_y().flex_wrap()
            .child(label(if selected {format!("{} × {} px · crop at {}, {}",n(region,"width",0.) as u32,n(region,"height",0.) as u32,n(region,"x",0.) as u32,n(region,"y",0.) as u32)}else{format!("{} × {} px · choose a region",w as u32,h as u32)},10.5,400.,p.muted).grow(1.))
            .child(btn("screenshot-undo","Undo",p,false,undo_ready && !s.pending,|s,_|undo(s,false)))
            .child(btn("screenshot-redo","Redo",p,false,redo_ready && !s.pending,|s,_|undo(s,true)))
            .child(btn("screenshot-delete-mark","Delete mark",p,false,e["selected"].is_u64() && !s.pending,|s,_|delete_selected(s)))
            .child(btn("screenshot-precision","Crop size",p,false,selected && !s.pending,|s,_|model::precision(s)))
            .child(btn("screenshot-whole-screen","Whole screen",p,false,!s.pending,|s,_|{
                remember(s);let [w,h]=dimensions(s);s.data["screenshot.editor"]["region"]=json!({"x":0,"y":0,"width":w,"height":h});choose_tool(s,"arrow");
            })))
        .child(row().gap(7.).center_y().flex_wrap()
            .child(label("View",10.5,450.,p.muted))
            .child(btn("screenshot-zoom-out","−",p,false,zoom>1. && !s.pending,|s,_|{let zoom=(n(&s.data["screenshot.editor"],"zoom",1.)-0.5).max(1.);s.data["screenshot.editor"]["zoom"]=json!(zoom);if zoom==1.{s.data["screenshot.editor"]["pan"]=json!([0.,0.]);}}))
            .child(label(format!("{}%",(zoom*100.) as u32),11.,450.,p.ink))
            .child(btn("screenshot-zoom-in","+",p,false,zoom<4. && !s.pending,|s,_|{let zoom=(n(&s.data["screenshot.editor"],"zoom",1.)+0.5).min(4.);s.data["screenshot.editor"]["zoom"]=json!(zoom);}))
            .child(btn("screenshot-pan","Pan",p,tool=="pan",zoom>1.,|s,_|choose_tool(s,"pan")))
            .child(link("screenshot-reset-view","Fit capture",p,|s,_|{s.data["screenshot.editor"]["zoom"]=json!(1.);s.data["screenshot.editor"]["pan"]=json!([0.,0.]);})))
        .child(label("A arrow · B box · P pen · H highlight · T text · N step · C crop · V move · Ctrl/Cmd+Z undo · Enter keep · Esc cancel",10.,400.,p.muted));
    let selected_text = e["selected"]
        .as_u64()
        .and_then(|index| e["annotations"].get(index as usize))
        .is_some_and(|a| a["tool"] == "text");
    if tool == "move" && selected_text {
        content = content.child(
            row()
                .gap(8.)
                .center_y()
                .child(
                    super::library::field(
                        s,
                        p,
                        "screenshot-label",
                        "Edit the selected label",
                        false,
                    )
                    .grow(1.),
                )
                .child(btn(
                    "screenshot-update-label",
                    "Update label",
                    p,
                    true,
                    !s.pending,
                    |s, _| {
                        let Some(index) = s.data["screenshot.editor"]["selected"]
                            .as_u64()
                            .map(|i| i as usize)
                        else {
                            return;
                        };
                        let label = s.edit("screenshot-label").text.clone();
                        if label.trim().is_empty() {
                            s.error = Some("Add a few words for the label.".into());
                            return;
                        }
                        remember(s);
                        s.data["screenshot.editor"]["annotations"][index]["text"] = json!(label);
                    },
                )),
        );
    }
    if e["precision"] == true {
        let mut fields = row().gap(8.).flex_wrap();
        for (key, title) in [
            ("x", "Left"),
            ("y", "Top"),
            ("width", "Width"),
            ("height", "Height"),
        ] {
            fields = fields.child(
                col()
                    .grow(1.)
                    .min_w(100.)
                    .gap(5.)
                    .child(label(title, 10.5, 500., p.muted))
                    .child(super::library::field(
                        s,
                        p,
                        &format!("screenshot-crop-{key}"),
                        title,
                        false,
                    )),
            );
        }
        content = content.child(
            card(p).gap(12.).child(fields).child(
                row()
                    .gap(8.)
                    .flex_wrap()
                    .child(btn(
                        "screenshot-apply-crop",
                        "Apply crop",
                        p,
                        true,
                        !s.pending,
                        |s, _| model::apply_precision(s),
                    ))
                    .child(btn(
                        "screenshot-cancel-crop",
                        "Cancel",
                        p,
                        false,
                        true,
                        |s, _| s.data["screenshot.editor"]["precision"] = json!(false),
                    )),
            ),
        );
    }
    if tool == "window" {
        if let Some(windows) = request["windows"].as_array() {
            let mut choices = card(p)
                .gap(8.)
                .child(label("Choose a window", 13., 550., p.ink));
            for window in windows.iter().take(60) {
                let selected = window.clone();
                let id = text(window, "id", "");
                choices = choices.child(btn(
                    format!("screenshot-window-{id}"),
                    text(window, "title", "Untitled window"),
                    p,
                    false,
                    !s.pending,
                    move |s, _| choose_window(s, &selected),
                ));
            }
            content = content.child(choices);
        }
    }
    content
}
