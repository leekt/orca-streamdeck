pub fn age(ms: u64) -> String {
    let seconds = ms / 1000;
    if seconds < 60 {
        format!("{seconds}s")
    } else if seconds < 3600 {
        format!("{}m", seconds / 60)
    } else if seconds < 86400 {
        format!("{}h", seconds / 3600)
    } else {
        format!("{}d", seconds / 86400)
    }
}

use crate::{approval::Arm, model::*};
use anyhow::{Context, Result};
use fontdue::{
    Font, FontSettings,
    layout::{CoordinateSystem, Layout, LayoutSettings, TextStyle},
};
use image::{Rgb, RgbImage};
use md5::{Digest, Md5};
use std::{fs, path::Path};

pub type Color = [u8; 3];
pub const AMBER: Color = [215, 140, 20];
pub const GRAY: Color = [60, 60, 66];
pub fn color(state: State) -> Color {
    match state {
        State::Blocked => AMBER,
        State::Working => [30, 90, 190],
        State::Done => [35, 130, 70],
        State::Idle => [210, 214, 222],
        State::Unknown => [90, 90, 90],
    }
}
fn foreground(bg: Color) -> Color {
    if 299 * bg[0] as u32 + 587 * bg[1] as u32 + 114 * bg[2] as u32 > 150000 {
        [20; 3]
    } else {
        [255; 3]
    }
}
fn shorten(text: &str, length: usize) -> String {
    text.chars().take(length).collect()
}
fn hsv(h: f32, s: f32, v: f32) -> Color {
    let f = h * 6.0;
    let i = f.floor() as u32;
    let f = f.fract();
    let (p, q, t) = (v * (1.0 - s), v * (1.0 - f * s), v * (1.0 - (1.0 - f) * s));
    let c = match i % 6 {
        0 => [v, t, p],
        1 => [q, v, p],
        2 => [p, v, t],
        3 => [p, q, v],
        4 => [t, p, v],
        _ => [v, p, q],
    };
    c.map(|x| (x * 255.0).round() as u8)
}
pub struct Renderer {
    font: Font,
    pub width: u32,
    pub height: u32,
}
impl Renderer {
    pub fn new(width: u32, height: u32) -> Result<Self> {
        let paths = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ];
        let bytes = paths
            .iter()
            .find_map(|p| fs::read(p).ok())
            .context("No usable system font found")?;
        let font =
            Font::from_bytes(bytes, FontSettings::default()).map_err(|e| anyhow::anyhow!(e))?;
        Ok(Self {
            font,
            width,
            height,
        })
    }
    pub fn blank(&self, color: Color) -> RgbImage {
        RgbImage::from_pixel(self.width, self.height, Rgb(color))
    }
    // Proportional coordinates keep the layout legible across Mini, MK.2 and XL.
    #[allow(clippy::too_many_arguments)]
    fn text(
        &self,
        image: &mut RgbImage,
        text: &str,
        x: f32,
        y: f32,
        size: f32,
        fg: Color,
        align: f32,
    ) {
        let mut px = (self.width as f32 * size).round().max(7.0);
        let mut layout = Layout::new(CoordinateSystem::PositiveYDown);
        let mut width;
        loop {
            layout.reset(&LayoutSettings::default());
            layout.append(&[&self.font], &TextStyle::new(text, px, 0));
            width = layout
                .glyphs()
                .iter()
                .map(|g| g.x + g.width as f32)
                .fold(0.0, f32::max);
            if width <= self.width as f32 * 0.92 || px <= 7.0 {
                break;
            }
            px -= 1.0;
        }
        let top = layout
            .glyphs()
            .iter()
            .filter(|g| g.height > 0)
            .map(|g| g.y)
            .fold(f32::MAX, f32::min);
        let bottom = layout
            .glyphs()
            .iter()
            .map(|g| g.y + g.height as f32)
            .fold(0.0, f32::max);
        let offset_x = x * self.width as f32 - width * align;
        let offset_y = y * self.height as f32 - (top + bottom) / 2.0;
        for glyph in layout.glyphs() {
            let (_, bitmap) = self.font.rasterize_config(glyph.key);
            for gy in 0..glyph.height {
                for gx in 0..glyph.width {
                    let (x, y) = (
                        (offset_x + glyph.x).round() as i32 + gx as i32,
                        (offset_y + glyph.y).round() as i32 + gy as i32,
                    );
                    if x < 0 || y < 0 || x >= self.width as i32 || y >= self.height as i32 {
                        continue;
                    }
                    let alpha = bitmap[gy * glyph.width + gx] as u32;
                    let pixel = image.get_pixel_mut(x as u32, y as u32);
                    for (channel, fg) in pixel.0.iter_mut().zip(fg) {
                        *channel =
                            ((fg as u32 * alpha + *channel as u32 * (255 - alpha)) / 255) as u8;
                    }
                }
            }
        }
    }
    pub fn tile(&self, item: Option<&Item>, now: u64, wash: Color) -> RgbImage {
        let Some(item) = item else {
            return self.blank(wash.map(|c| (c as f32 * 0.35) as u8));
        };
        let bg = color(item.state);
        let fg = foreground(bg);
        let mut img = self.blank(bg);
        let hash = Md5::digest(format!(
            "{}:{}:{}",
            item.machine_id, item.session, item.label
        ));
        let accent = hsv(hash[15] as f32 / 255.0, 0.6, 0.9);
        let size = self.width.min(self.height) as f32 * if item.show_machine { 0.32 } else { 0.42 };
        let cell = size * 0.76 / 5.0;
        let left = (self.width as f32 - size) / 2.0 + size * 0.12;
        let top = self.height as f32 * 0.13 + size * 0.12;
        for i in 0..15 {
            if hash[i] & 1 == 1 {
                for column in [i / 5, 4 - i / 5] {
                    let x = (left + column as f32 * cell) as u32;
                    let y = (top + (i % 5) as f32 * cell) as u32;
                    for dy in 0..cell.ceil() as u32 {
                        for dx in 0..cell.ceil() as u32 {
                            if x + dx < self.width && y + dy < self.height {
                                img.put_pixel(x + dx, y + dy, Rgb(accent));
                            }
                        }
                    }
                }
            }
        }
        if item.show_machine {
            self.text(
                &mut img,
                &shorten(&item.machine_label, 14),
                0.5,
                0.53,
                0.105,
                fg,
                0.5,
            );
        }
        self.text(
            &mut img,
            &shorten(&item.label, 12),
            0.5,
            0.68,
            0.13,
            fg,
            0.5,
        );
        self.text(&mut img, &shorten(&item.sub, 14), 0.5, 0.86, 0.115, fg, 0.5);
        self.text(
            &mut img,
            &shorten(&item.agent_type, 6),
            0.04,
            0.09,
            0.12,
            fg,
            0.0,
        );
        if item.state_since > 0 {
            self.text(
                &mut img,
                &age(now.saturating_sub(item.state_since)),
                0.96,
                0.09,
                0.12,
                fg,
                1.0,
            );
        }
        if item.unread {
            let r = (self.width as f32 * 0.045).max(2.0) as i32;
            let cx = self.width as i32 - r - 4;
            let cy = self.height as i32 - r - 4;
            for y in -r..=r {
                for x in -r..=r {
                    if x * x + y * y <= r * r {
                        img.put_pixel((cx + x) as u32, (cy + y) as u32, Rgb(fg));
                    }
                }
            }
        }
        if item.machine_id != "local" || item.session != "default" {
            for y in 0..self.height {
                for x in self.width - 3..self.width {
                    img.put_pixel(x, y, Rgb(accent));
                }
            }
        }
        img
    }
    pub fn action(&self, label: &str, sub: &str, mut color: Color, enabled: bool) -> RgbImage {
        if !enabled {
            color = color.map(|c| (c as f32 * 0.3) as u8);
        }
        let fg = if enabled {
            foreground(color)
        } else {
            [120, 120, 126]
        };
        let mut img = self.blank(color);
        self.text(
            &mut img,
            label,
            0.5,
            if sub.is_empty() { 0.5 } else { 0.38 },
            0.19,
            fg,
            0.5,
        );
        self.text(&mut img, &shorten(sub, 14), 0.5, 0.68, 0.12, fg, 0.5);
        img
    }
    pub fn status(
        &self,
        frame: &Frame,
        page: usize,
        pages: usize,
        arm: Option<&Arm>,
        now: u64,
    ) -> RgbImage {
        let count = frame.blocked();
        let mut img = self.blank(if count > 0 {
            [200, 40, 40]
        } else {
            [40, 42, 50]
        });
        let offline: Vec<_> = frame
            .machines
            .iter()
            .filter(|m| m.state == Connection::Offline)
            .collect();
        if count > 0 {
            self.text(&mut img, &count.to_string(), 0.5, 0.4, 0.4, [255; 3], 0.5);
            self.text(&mut img, "NEED YOU", 0.5, 0.73, 0.13, [255; 3], 0.5);
        } else if !offline.is_empty() {
            let label = if offline.len() == 1 {
                offline[0].label.clone()
            } else {
                format!("{} machines", offline.len())
            };
            self.text(&mut img, &shorten(&label, 14), 0.5, 0.42, 0.12, AMBER, 0.5);
            self.text(&mut img, "OFFLINE", 0.5, 0.64, 0.14, AMBER, 0.5);
        } else {
            let connecting = frame.machines.is_empty()
                || frame
                    .machines
                    .iter()
                    .any(|m| m.state == Connection::Connecting);
            self.text(
                &mut img,
                if connecting {
                    "connecting"
                } else {
                    "all clear"
                },
                0.5,
                0.5,
                0.15,
                [150, 155, 165],
                0.5,
            );
        }
        self.text(
            &mut img,
            &format!("{}/{} ONLINE", frame.online(), frame.machines.len()),
            0.5,
            0.93,
            0.11,
            [180, 190, 200],
            0.5,
        );
        if pages > 1 {
            self.text(
                &mut img,
                &format!("{}/{}", page + 1, pages),
                0.97,
                0.09,
                0.12,
                [230, 230, 235],
                1.0,
            );
        }
        if let Some(arm) = arm {
            self.text(
                &mut img,
                &arm.badge(now as f64 / 1000.0),
                0.04,
                0.09,
                0.12,
                AMBER,
                0.0,
            );
        }
        img
    }
    pub fn preview(path: &Path) -> Result<()> {
        let renderer = Self::new(80, 80)?;
        let mut canvas = RgbImage::from_pixel(272, 184, Rgb([15; 3]));
        let source = Source {
            id: "mini".into(),
            label: "Mac Mini Dev".into(),
            target: Some("herdr-remote".into()),
            session: "default".into(),
        };
        let snapshot = serde_json::json!({"agents":[{"pane_id":"p","terminal_id":"t","agent":"codex","agent_session":{"id":"preview"},"agent_status":"blocked","cwd":"/workspace/herdr-streamdeck","title":"Rust rewrite"}]});
        let mut item = build_items(&snapshot, &source)?.remove(0);
        item.show_machine = true;
        item.state_since = now_ms() - 120000;
        let tiles = [
            renderer.tile(Some(&item), now_ms(), [0; 3]),
            renderer.action("FOCUS", "Mac Mini Dev", [30, 90, 190], true),
            renderer.action("APPRV", "command", AMBER, true),
            renderer.action("AUTO", "off", AMBER, false),
            renderer.action("DIFFS", "changes", GRAY, true),
            renderer.status(
                &Frame {
                    agents: vec![item],
                    machines: vec![MachineStatus {
                        id: "mini".into(),
                        label: "Mac Mini Dev".into(),
                        state: Connection::Online,
                        error: String::new(),
                    }],
                },
                0,
                1,
                None,
                now_ms(),
            ),
        ];
        for (i, tile) in tiles.iter().enumerate() {
            image::imageops::replace(
                &mut canvas,
                tile,
                8 + (i % 3) as i64 * 88,
                8 + (i / 3) as i64 * 88,
            );
        }
        canvas.save(path)?;
        Ok(())
    }
}
