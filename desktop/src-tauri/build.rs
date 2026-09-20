fn main() {
    // 根因修复：tauri-build 默认不盯前端目录——改了 ui/ 但 Rust 没变时，
    // 嵌入资源走增量缓存，界面就「不更新」。显式声明依赖。
    println!("cargo:rerun-if-changed=../ui");
    tauri_build::build()
}
