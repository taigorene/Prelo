--[[
docx-media.lua - puts the document's images into pandoc's media bag.

Applied when writing DOCX. The conversion passes each distinct image once, as a
data: URI in the "prelo-media" metadata map, and every picture refers to it as
"prelo-media:<key>"; an image used many times is therefore decoded and stored once.
The map is removed from the metadata (it would otherwise end up in the document
properties). Pictures still holding a data: URI are handled the same way; without
this filter pandoc would copy the whole URI into the picture's description. Decoding
a data: URI performs no I/O, so this works under --sandbox.
]]

local MEDIA_KEY = 'prelo-media'
local PREFIX = 'prelo-media:'

local EXTENSIONS = {
  ['image/png'] = 'png',
  ['image/jpeg'] = 'jpg',
  ['image/gif'] = 'gif',
  ['image/svg+xml'] = 'svg',
  ['image/webp'] = 'webp',
  ['image/bmp'] = 'bmp',
}

function Pandoc(doc)
  local media = doc.meta[MEDIA_KEY] or {}
  doc.meta[MEDIA_KEY] = nil
  local names = {}
  local count = 0

  local function store(key, uri)
    local name = names[key]
    if name then
      return name
    end
    local ok, mime, contents = pcall(pandoc.mediabag.fetch, uri)
    if not ok or not contents then
      return nil
    end
    count = count + 1
    name = string.format('image%d.%s', count, EXTENSIONS[mime] or 'bin')
    pandoc.mediabag.insert(name, mime, contents)
    names[key] = name
    return name
  end

  return doc:walk({
    Image = function(img)
      local src = img.src
      local key, uri
      if src:sub(1, #PREFIX) == PREFIX then
        key = src:sub(#PREFIX + 1)
        if media[key] == nil then
          return nil
        end
        uri = pandoc.utils.stringify(media[key])
      elseif src:sub(1, 5):lower() == 'data:' then
        key, uri = src, src
      else
        return nil
      end
      local name = store(key, uri)
      if not name then
        return nil
      end
      img.src = name
      return img
    end,
  })
end
