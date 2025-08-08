from rgbmatrix import RGBMatrix, RGBMatrixOptions

def matrixOptions():
    # Configuration for the matrix
    options = RGBMatrixOptions()
    options.rows = 64
    options.cols = 64
    options.chain_length = 8
    options.parallel = 3
    options.hardware_mapping = 'regular'  # If you have an Adafruit HAT: 'adafruit-hat'
    options.pixel_mapper_config = 'Rotate:180;U-mapper' # ;Rotate:270
    options.gpio_slowdown = 5
    return options
