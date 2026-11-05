import Foundation
import AppKit
import PDFKit
import Vision

let url = URL(fileURLWithPath: CommandLine.arguments[1])
var images: [CGImage] = []
if let document = PDFDocument(url: url) {
    for index in 0..<document.pageCount {
        guard let page = document.page(at: index) else { continue }
        let bounds = page.bounds(for: .mediaBox)
        let image = page.thumbnail(of: NSSize(width: bounds.width * 2, height: bounds.height * 2), for: .mediaBox)
        var rect = NSRect(origin: .zero, size: image.size)
        if let cg = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) { images.append(cg) }
    }
} else if let image = NSImage(contentsOf: url) {
    var rect = NSRect(origin: .zero, size: image.size)
    if let cg = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) { images.append(cg) }
}
var pages: [String] = []
for image in images {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["ru-RU", "en-US"]
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: image).perform([request])
    pages.append((request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n"))
}
let data = try JSONSerialization.data(withJSONObject: pages)
print(String(data: data, encoding: .utf8)!)
