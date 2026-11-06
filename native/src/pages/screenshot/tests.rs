use super::*;
use crate::{Page, State, view};
use rune::{Env, ImageCache, Key, KeyEvent, Runtime};
use serde_json::json;
use std::sync::Arc;

fn editor(width: f32, mode: &str) -> Runtime<State> {
    let mut s = State::new();
    s.demo = true;
    s.snapshot.ready = true;
    s.image_cache = Some(ImageCache::new(Arc::new(|| {})));
    let image = s
        .image_cache
        .as_ref()
        .unwrap()
        .rgba(1200, 600, vec![245u8; 1200 * 600 * 4]);
    image.wait().unwrap();
    begin_screenshot(
        &mut s,
        &json!({"id":"capture-test","image_path":"fixture-image","width":1200,"height":600,"mode":mode,
        "windows":[{"id":"browser","title":"Project notes","x":100,"y":80,"width":500,"height":350}]}),
    );
    s.images.insert("fixture-image".into(), image);
    let mut rt = Runtime::new(
        s,
        view,
        (width, 980.),
        Env {
            system_dark: false,
            scale: 1.,
        },
    );
    settle(&mut rt);
    rt
}
fn settle(rt: &mut Runtime<State>) {
    for _ in 0..12 {
        rt.frame(1. / 60.);
    }
}
fn tap(rt: &mut Runtime<State>, key: &str) {
    for _ in 0..8 {
        let (_, y) = rt.find(key).unwrap_or_else(|| panic!("missing {key}"));
        if y > 90. && y < 900. {
            break;
        }
        rt.wheel(600., 450., 450. - y);
        settle(rt);
    }
    let (x, y) = rt.find(key).unwrap_or_else(|| panic!("missing {key}"));
    rt.pointer_down(x, y);
    rt.pointer_up(x, y);
    settle(rt);
}
fn screen_point(rt: &Runtime<State>, p: [f32; 2]) -> [f32; 2] {
    let (r, _) = rt.presented("screenshot-canvas").unwrap();
    let transform = model::fit(model::dimensions(&rt.state), [r[2], r[3]]);
    [
        r[0] + transform[0] + p[0] * transform[2],
        r[1] + transform[1] + p[1] * transform[2],
    ]
}
fn draw(rt: &mut Runtime<State>, from: [f32; 2], to: [f32; 2]) {
    let a = screen_point(rt, from);
    let b = screen_point(rt, to);
    rt.pointer_down(a[0], a[1]);
    settle(rt);
    rt.pointer_move((a[0] + b[0]) / 2., (a[1] + b[1]) / 2.);
    settle(rt);
    rt.pointer_move(b[0], b[1]);
    rt.pointer_up(b[0], b[1]);
    settle(rt);
}
#[test]
fn region_drag_uses_original_pixels_with_letterboxing() {
    let mut rt = editor(1220., "region");
    draw(&mut rt, [120., 60.], [960., 480.]);
    let region = &rt.state.data["screenshot.editor"]["region"];
    assert_eq!(
        region,
        &json!({"x":120.,"y":60.,"width":840.,"height":420.})
    );
    assert_eq!(rt.state.data["screenshot.editor"]["tool"], "arrow");
    tap(&mut rt, "screenshot-keep");
    assert_eq!(
        rt.state.data["screenshot.submitted"]["region"],
        json!({"x":120.,"y":60.,"width":840.,"height":420.})
    );
    assert_eq!(rt.state.page, Page::Record);
}
#[test]
fn window_selection_crops_to_actual_window_bounds() {
    let mut rt = editor(720., "window");
    let p = screen_point(&rt, [250., 200.]);
    rt.pointer_down(p[0], p[1]);
    rt.pointer_up(p[0], p[1]);
    settle(&mut rt);
    assert_eq!(
        rt.state.data["screenshot.editor"]["region"],
        json!({"x":100.,"y":80.,"width":500.,"height":350.})
    );
}
#[test]
fn annotation_tools_save_real_geometry_and_undo_redo() {
    let mut rt = editor(1220., "screen");
    draw(&mut rt, [200., 200.], [800., 360.]);
    tap(&mut rt, "screenshot-tool-rect");
    draw(&mut rt, [100., 100.], [500., 300.]);
    tap(&mut rt, "screenshot-tool-freehand");
    draw(&mut rt, [150., 400.], [900., 450.]);
    tap(&mut rt, "screenshot-tool-highlight");
    draw(&mut rt, [220., 200.], [600., 260.]);
    let annotations = rt.state.data["screenshot.editor"]["annotations"]
        .as_array()
        .unwrap();
    assert_eq!(annotations.len(), 4);
    assert!((annotations[0]["points"][0]["x"].as_f64().unwrap() - 200.).abs() < 0.01);
    assert!((annotations[0]["points"][0]["y"].as_f64().unwrap() - 200.).abs() < 0.01);
    assert!(annotations[2]["points"].as_array().unwrap().len() > 2);
    assert!(
        annotations[3]["width"].as_f64().unwrap() > annotations[2]["width"].as_f64().unwrap() * 4.
    );
    tap(&mut rt, "screenshot-undo");
    assert_eq!(
        rt.state.data["screenshot.editor"]["annotations"]
            .as_array()
            .unwrap()
            .len(),
        3
    );
    tap(&mut rt, "screenshot-redo");
    assert_eq!(
        rt.state.data["screenshot.editor"]["annotations"]
            .as_array()
            .unwrap()
            .len(),
        4
    );
}
#[test]
fn labels_steps_and_mark_movement_remain_editable() {
    let mut rt = editor(1220., "screen");
    tap(&mut rt, "screenshot-tool-text");
    rt.update(|s, _| s.edit("screenshot-label").text = "Choose the microphone".into());
    settle(&mut rt);
    draw(&mut rt, [200., 200.], [200., 200.]);
    tap(&mut rt, "screenshot-tool-step");
    draw(&mut rt, [150., 200.], [150., 200.]);
    assert_eq!(
        rt.state.data["screenshot.editor"]["annotations"][1]["text"],
        "1"
    );
    tap(&mut rt, "screenshot-tool-move");
    draw(&mut rt, [200., 205.], [300., 305.]);
    assert!(
        (rt.state.data["screenshot.editor"]["annotations"][0]["points"][0]["x"]
            .as_f64()
            .unwrap()
            - 300.)
            .abs()
            < 0.01
    );
    assert!(
        (rt.state.data["screenshot.editor"]["annotations"][0]["points"][0]["y"]
            .as_f64()
            .unwrap()
            - 300.)
            .abs()
            < 0.01
    );
    rt.update(|s, _| s.edit("screenshot-label").text = "A revised label".into());
    settle(&mut rt);
    tap(&mut rt, "screenshot-update-label");
    assert_eq!(
        rt.state.data["screenshot.editor"]["annotations"][0]["text"],
        "A revised label"
    );
    tap(&mut rt, "screenshot-delete-mark");
    assert_eq!(
        rt.state.data["screenshot.editor"]["annotations"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}
#[test]
fn crop_precision_clamps_to_source_and_escape_cancels() {
    let mut rt = editor(720., "screen");
    tap(&mut rt, "screenshot-precision");
    rt.update(|s, _| {
        s.edit("screenshot-crop-x").text = "1100".into();
        s.edit("screenshot-crop-y").text = "500".into();
        s.edit("screenshot-crop-width").text = "500".into();
        s.edit("screenshot-crop-height").text = "500".into();
    });
    settle(&mut rt);
    tap(&mut rt, "screenshot-apply-crop");
    assert_eq!(
        rt.state.data["screenshot.editor"]["region"],
        json!({"x":1100.,"y":500.,"width":100.,"height":100.})
    );
    rt.update(|s, _| {
        assert!(screenshot_key(s, &KeyEvent::from(Key::Escape)));
    });
    settle(&mut rt);
    assert_eq!(rt.state.data["screenshot.submitted"]["cancelled"], true);
}

#[test]
fn screenshot_completion_returns_to_scratchpad() {
    let mut s = State::new();
    s.demo = true;
    s.page = Page::Scratchpad;
    begin_screenshot(
        &mut s,
        &json!({"id":"scratchpad-shot","width":1200,"height":600,"mode":"region"}),
    );
    assert_eq!(s.page, Page::Screenshot);
    screenshot_finished(&mut s);
    assert_eq!(s.page, Page::Scratchpad);
    assert!(s.data["screenshot.choose"].is_null());
}

#[test]
fn zoom_and_pan_preserve_original_pixel_crop_coordinates() {
    let mut rt = editor(1220., "screen");
    tap(&mut rt, "screenshot-zoom-in");
    tap(&mut rt, "screenshot-pan");
    draw(&mut rt, [550., 250.], [600., 280.]);
    assert!(
        rt.state.data["screenshot.editor"]["pan"][0]
            .as_f64()
            .unwrap()
            > 0.
    );
    tap(&mut rt, "screenshot-tool-select");
    draw(&mut rt, [500., 200.], [700., 400.]);
    assert_eq!(
        rt.state.data["screenshot.editor"]["region"],
        json!({"x":500.,"y":200.,"width":200.,"height":200.})
    );
}

#[test]
fn crop_corner_handles_resize_without_drawing_a_mark() {
    let mut rt = editor(1220., "region");
    draw(&mut rt, [120., 60.], [960., 480.]);
    draw(&mut rt, [960., 480.], [1050., 540.]);
    assert_eq!(
        rt.state.data["screenshot.editor"]["region"],
        json!({"x":120.,"y":60.,"width":930.,"height":480.})
    );
    assert!(
        rt.state.data["screenshot.editor"]["annotations"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    tap(&mut rt, "screenshot-undo");
    assert_eq!(
        rt.state.data["screenshot.editor"]["region"],
        json!({"x":120.,"y":60.,"width":840.,"height":420.})
    );
}
#[test]
fn every_capture_tool_stays_inside_compact_and_wide_windows() {
    for width in [720., 1220.] {
        for mode in ["region", "window", "screen"] {
            let mut rt = editor(width, mode);
            for tool in ["select", "window", "text", "move"] {
                tap(&mut rt, &format!("screenshot-tool-{tool}"));
                for node in rt
                    .elements()
                    .iter()
                    .filter(|node| node.text.is_some() && !node.exiting)
                {
                    assert!(
                        node.rect[0] >= -0.5 && node.rect[0] + node.rect[2] <= width + 0.5,
                        "{mode}/{tool} at {width}: {:?} {:?}",
                        node.text,
                        node.rect
                    );
                }
            }
        }
    }
}

#[test]
#[ignore = "Manual screenshot review; requires a captured PNG and a GPU or software Vulkan"]
fn render_screenshot_editor_for_visual_review() {
    use rune::render::{Gpu, OFFSCREEN_FORMAT, Offscreen, Renderer, write_png};
    let source = std::env::var("OPENWHISPER_UI_SOURCE_CAPTURE").unwrap_or_else(|_| {
        format!(
            "{}/screenshots/studio-light.png",
            env!("CARGO_MANIFEST_DIR")
        )
    });
    let output = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("screenshots");
    let gpu = Gpu::headless();
    for width in [720u32, 1220u32] {
        let mut s = State::new();
        crate::state::SCENES
            .iter()
            .find(|(name, _)| *name == "studio")
            .unwrap()
            .1(&mut s);
        s.image_cache = Some(ImageCache::new(Arc::new(|| {})));
        let image = s.image_cache.as_ref().unwrap().file(&source, 1200., 1.);
        let [sw, sh] = image.wait().unwrap();
        begin_screenshot(
            &mut s,
            &json!({"id":"visual-review","image_path":source,"width":sw,"height":sh,"mode":"screen","windows":[]}),
        );
        s.images.insert(source.clone(), image);
        s.data["screenshot.editor"]["region"] = json!({"x":200,"y":110,"width":900.min(sw.saturating_sub(200)),"height":590.min(sh.saturating_sub(110))});
        let mut rt = Runtime::new(
            s,
            view,
            (width as f32, 980.),
            Env {
                system_dark: false,
                scale: 1.,
            },
        );
        settle(&mut rt);
        draw(&mut rt, [350., 370.], [600., 280.]);
        tap(&mut rt, "screenshot-tool-rect");
        draw(&mut rt, [480., 210.], [850., 480.]);
        tap(&mut rt, "screenshot-tool-text");
        rt.update(|s, _| s.edit("screenshot-label").text = "Keep the controls close".into());
        settle(&mut rt);
        draw(&mut rt, [450., 145.], [450., 145.]);
        tap(&mut rt, "screenshot-tool-step");
        draw(&mut rt, [310., 365.], [310., 365.]);
        tap(&mut rt, "screenshot-tool-arrow");
        rt.wheel(600., 450., 10000.);
        settle(&mut rt);
        let mut renderer = Renderer::new(&gpu.device, OFFSCREEN_FORMAT);
        renderer.prepare(
            &gpu.device,
            &gpu.queue,
            &mut rt.text.atlas,
            &rt.instances,
            (width, 980),
        );
        renderer.upload_images(&gpu.device, &gpu.queue, &mut rt.images);
        let target = Offscreen::new(&gpu.device, width, 980);
        let mut encoder = gpu.device.create_command_encoder(&Default::default());
        renderer.draw(&mut encoder, &target.view, [1., 1., 1., 1.]);
        target.copy(&mut encoder);
        gpu.queue.submit([encoder.finish()]);
        let rgba = target.read(&gpu.device);
        write_png(
            &output.join(format!("screenshot-editor-{width}.png")),
            width,
            980,
            &rgba,
        )
        .unwrap();
    }
}
