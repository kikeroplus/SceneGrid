# SceneGrid Icon

- `scenegrid.png`: original transparent raster artwork, generated with the built-in image generation tool.
- `scenegrid.ico`: Windows icon containing 16, 20, 24, 32, 40, 48, 64, 128 and 256 pixel frames.
- `build_icon.ps1`: lossless-format packaging of resized PNG frames into ICO, using Windows System.Drawing. Run from PowerShell to rebuild the ICO after replacing the source PNG.

The ICO is embedded in the EXE and bundled as a runtime resource for all application windows.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\assets\build_icon.ps1
```

The execution policy override applies only to this conversion process, not to system settings.

## Generation Prompt

Use case: logo-brand. Asset type: production Windows desktop application icon for SceneGrid, a tool to review videos as a 4 by 4 contact sheet and assign star ratings. Create one square icon only, centered, transparent background. A bold charcoal rounded-square video/contact-sheet frame containing an exact 4 by 4 array of clean small rectangular thumbnail tiles, predominantly vivid turquoise and off-white with a few muted coral tiles. One prominent simple golden five-point rating star overlaps the bottom-right of the frame, with a crisp dark separation outline so it remains legible at 32 pixels. Flat polished graphic design, thick clean silhouettes, very restrained depth, high contrast, no intricate thumbnail pictures. The design occupies about 88% of the canvas. No text, no letters, no wordmark, no watermark, no surrounding scene, no mockup, no glow, no extra icons. Preserve real transparent alpha outside the icon. Square 1024 by 1024.
