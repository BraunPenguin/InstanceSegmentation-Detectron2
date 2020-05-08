import os
import pickle
import joblib
import contextlib
from tqdm import tqdm
import multiprocessing
import matplotlib.pyplot as plt
import matplotlib.path as mpltPath
import matplotlib as mpl
import matplotlib.patches as patches
from shapely.geometry import Point, LineString, MultiLineString, Polygon
from shapely import affinity
import numpy as np
from os import path
from PIL import Image, ImageOps
from skimage.measure import label, regionprops, find_contours
from tkinter import Tk, filedialog
import sys
from collections import OrderedDict
from polylidar import extractPlanesAndPolygons
from polylidarutil import (generate_test_points, plot_points, plot_triangles, get_estimated_lmax,
                           plot_triangle_meshes, get_triangles_from_he, get_plane_triangles, plot_polygons, get_point)

from polylidar import extractPolygons
from MinimumBoundingBox import MinimumBoundingBox
# from detectron2 import model_zoo
# from detectron2.engine import DefaultPredictor
# from detectron2.config import get_cfg
# from detectron2.utils.visualizer import Visualizer, ColorMode
# from detectron2.data import MetadataCatalog, DatasetCatalog, build_detection_test_loader
# from detectron2.evaluation import COCOEvaluator, inference_on_dataset
# from detectron2.utils.logger import setup_logger
showPlots = False
showBoundingBoxPlots = False
plotPolylidar = False
isVerticalSubSection = True
parallelProcessing = False


@contextlib.contextmanager
def tqdm_joblib(tqdm_object):
    """Context manager to patch joblib to report into tqdm progress bar given as argument"""
    class TqdmBatchCompletionCallback:
        def __init__(self, time, index, parallel):
            self.index = index
            self.parallel = parallel

        def __call__(self, index):
            tqdm_object.update()
            if self.parallel._original_iterator is not None:
                self.parallel.dispatch_next()

    old_batch_callback = joblib.parallel.BatchCompletionCallBack
    joblib.parallel.BatchCompletionCallBack = TqdmBatchCompletionCallback
    try:
        yield tqdm_object
    finally:
        joblib.parallel.BatchCompletionCallBack = old_batch_callback
        tqdm_object.close()


def createPolygonFromCoordList(coordList, blankMask):
    # Lets try to find contours and make that a polygon which we need later, and gives us minimum_rotated_rectangle as well
    for coord in coordList:
        blankMask[coord[0]][coord[1]] = 255
    maskCImage = Image.fromarray(np.uint8(blankMask))
    # This fixes the corner issue of diagonally cutting across the mask since edge pixels had no neighboring black pixels
    maskCImage_bordered = ImageOps.expand(maskCImage, border=1)
    contour = find_contours(maskCImage_bordered, 0.5, positive_orientation='low')[0]

    # Flip from (row, col) representation to (x, y)
    # and subtract the padding pixel (not doing that, we didn't buffer)

    # The 2nd line is needed for the correct orientation in the TrainNewData.py file
    # If wanting to showPlots here and get correct orientation, need to change something in the plotting code
    # contour[:, 0], contour[:, 1] = contour[:, 1], sub_mask.size[1] - contour[:, 0].copy()
    contour[:, 0], contour[:, 1] = contour[:, 1], contour[:, 0].copy()

    # Make a polygon and simplify it
    poly = Polygon(contour)
    poly = poly.simplify(1.0, preserve_topology=False)  # should use preserve_topology=True?
    return poly


# @profile
def centerXPercentofWire(npMaskFunc, percentSize, isVerticalSubSection: bool):
    assert 0 <= percentSize <= 1, "Percent size of section has to be between 0 and 1"
    assert isinstance(isVerticalSubSection,
                      bool), "isVerticalSubSection must be a boolean, True if you want a vertical subsection, False if you want a horizontal subsection"
    label_image = label(npMaskFunc, connectivity=1)
    allRegionProperties = regionprops(label_image)
    subMask = np.zeros(npMaskFunc.shape)
    maskCTest = subMask.copy()
    largeRegionsNums = set()
    regionNum = 0
    for region in allRegionProperties:
        # Ignore small regions or if an instance got split into a major and minor part
        if region.area > 100:
            largeRegionsNums.add(regionNum)
        regionNum += 1
    if len(largeRegionsNums) == 1:
        region = allRegionProperties[list(largeRegionsNums)[0]]
        ymin, xmin, ymax, xmax = region.bbox

        # maskCords as [row, col] ie [y, x]
        maskCoords = region.coords
        flippedMaskCoords = [(entry[1], entry[0]) for entry in maskCoords]
        maskAngle = np.rad2deg(region.orientation)


        # pip install git+git://github.com/BraunMichael/MinimumBoundingBox.git@master
        # This is much slower
        # shapelyTestRect = MultiPoint(maskCoords).minimum_rotated_rectangle
        # This is a bit faster, but polylidar is faster still
        # maskPolygon = createPolygonFromCoordList(maskCoords, maskCTest)
        # This is actually faster for just the bounding box, but I think slower overall as we need the contour Polygon later
        outputMinimumBoundingBox = MinimumBoundingBox(maskCoords)

        # Will need to use Polygon subtraction to convert to subMask and final rotated bounding box
        # polylidarPolygon = extractPolygons(flippedMaskCoords)
        # Extracts planes and polygons, time
        points = np.array(flippedMaskCoords)
        polygons = extractPolygons(points)
        for poly in polygons:
            shell_coords = [get_point(pi, points) for pi in poly.shell]
            outline = Polygon(shell=shell_coords)
        if plotPolylidar:
            fig, ax = plt.subplots(figsize=(8, 8), nrows=1, ncols=1)
            # plot points
            plot_points(np.array(flippedMaskCoords), ax)
            # plot polygons...doesn't use 2nd argument
            plot_polygons(polygons, 0, np.array(flippedMaskCoords), ax)
            plt.axis('equal')
            plt.show()

        # Do this in isVerticalSubSection is False for length calculations...or maybe also everywhere for less restrictive overlap measures?
        mbbCenter = outputMinimumBoundingBox['rectangle_center']
        mbbLength = max(outputMinimumBoundingBox['length_orthogonal'], outputMinimumBoundingBox['length_parallel'])
        if isVerticalSubSection:
            mbbWidth = min(outputMinimumBoundingBox['length_orthogonal'], outputMinimumBoundingBox['length_parallel'])
        else:
            mbbWidth = min(outputMinimumBoundingBox['length_orthogonal'], outputMinimumBoundingBox['length_parallel']) * percentSize
        lowerLeft = (mbbCenter[1] - mbbWidth / 2, mbbCenter[0] + mbbLength / 2)
        lowerRight = (mbbCenter[1] + mbbWidth / 2, mbbCenter[0] + mbbLength / 2)
        upperRight = (mbbCenter[1] + mbbWidth / 2, mbbCenter[0] - mbbLength / 2)
        upperLeft = (mbbCenter[1] - mbbWidth / 2, mbbCenter[0] - mbbLength / 2)
        newMBB = Polygon([lowerLeft, lowerRight, upperRight, upperLeft])

        mbbRotation = outputMinimumBoundingBox['cardinal_angle_deg']
        rotatedNewMBB = affinity.rotate(newMBB, -mbbRotation)

        if showBoundingBoxPlots:
            fig = plt.figure(figsize=(15, 12))
            ax = fig.add_subplot(111)
            ax.imshow(npMaskFunc)
            r1 = patches.Rectangle((xmin, ymax), xmax-xmin, -(ymax-ymin), fill=False, edgecolor="blue", alpha=1, linewidth=1)
            r2 = patches.Rectangle(lowerLeft, mbbWidth, -mbbLength, fill=False, edgecolor="red", alpha=1, linewidth=1)

            t2 = mpl.transforms.Affine2D().rotate_deg_around(mbbCenter[1], mbbCenter[0], -mbbRotation) + ax.transData
            r2.set_transform(t2)
            ax.axis('equal')
            ax.add_patch(r1)
            ax.add_patch(r2)

            # 5, since we have 4 points for a rectangle but don't want to have 1st = 4th
            phi = -1 * np.linspace(0, 2*np.pi, 5)
            rgb_cycle = np.vstack((.5 * (1. + np.cos(phi)), .5 * (1. + np.cos(phi + 2 * np.pi / 3)), .5 * (1. + np.cos(phi - 2 * np.pi / 3)))).T
            plt.scatter(rotatedNewMBB.exterior.coords.xy[0][:-1], rotatedNewMBB.exterior.coords.xy[1][:-1], c=rgb_cycle[:4])
            plt.autoscale()
            plt.show()

        if isVerticalSubSection:
            originalbboxHeight = ymax - ymin
            newbboxHeight = originalbboxHeight * percentSize
            ymin = ymin + 0.5 * (originalbboxHeight - newbboxHeight)
            ymax = ymax - 0.5 * (originalbboxHeight - newbboxHeight)
        else:
            # Switch to rotated bounding box
            originalbboxWidth = xmax - xmin
            newbboxWidth = originalbboxWidth * percentSize
            xmin = xmin + 0.5 * (originalbboxWidth - newbboxWidth)
            xmax = xmax - 0.5 * (originalbboxWidth - newbboxWidth)

        newBoundingBoxPoly = bboxToPoly(xmin, ymin, xmax, ymax)


        subMaskCoords = []
        path = mpltPath.Path(newBoundingBoxPoly)
        subMaskCoordsBoolList = path.contains_points(flippedMaskCoords)

        for inPoly, coord in zip(subMaskCoordsBoolList, maskCoords):
            if inPoly:
                subMask[coord[0]][coord[1]] = 1
                subMaskCoords.append((coord[0], coord[1]))
        return subMask, subMaskCoords, maskAngle, rotatedNewMBB
    # else:
    return None, None, None, None


def fileHandling(annotationFileName):
    with open(annotationFileName, 'rb') as handle:
        fileContents = pickle.loads(handle.read())
    return fileContents


def bboxToPoly(xmin, ymin, xmax, ymax):
    return [[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]]


def makeSubMaskCoordsDict(maskCoords, isVerticalSubSection):
    # maskCoords are [row, col]
    assert isinstance(isVerticalSubSection,
                      bool), "isVerticalSubSection must be a boolean, True if you want a vertical subsection, False if you want a horizontal subsection"
    subMaskCoordsDict = {}

    for row, col in maskCoords:
        if isVerticalSubSection:
            if row not in subMaskCoordsDict:
                subMaskCoordsDict[row] = []
            subMaskCoordsDict[row].append(col)
        else:
            if col not in subMaskCoordsDict:
                subMaskCoordsDict[col] = []
            subMaskCoordsDict[col].append(row)
    return subMaskCoordsDict


def getFileOrDirList(fileOrFolder: str = 'file', titleStr: str = 'Choose a file', fileTypes: str = '*', initialDirOrFile: str = os.getcwd()):
    if os.path.isfile(initialDirOrFile) or os.path.isdir(initialDirOrFile):
        initialDir = os.path.split(initialDirOrFile)[0]
    else:
        initialDir = initialDirOrFile
    root = Tk()
    root.withdraw()
    assert fileOrFolder.lower() == 'file' or fileOrFolder.lower() == 'folder', "Only file or folder is an allowed string choice for fileOrFolder"
    if fileOrFolder.lower() == 'file':
        fileOrFolderList = filedialog.askopenfilename(initialdir=initialDir, title=titleStr,
                                                      filetypes=[(fileTypes + "file", fileTypes)])
    else:  # Must be folder from assert statement
        fileOrFolderList = filedialog.askdirectory(initialdir=initialDir, title=titleStr)
    if not fileOrFolderList:
        fileOrFolderList = initialDirOrFile
    root.destroy()
    return fileOrFolderList


# @profile
def isValidLine(boundingBoxDict, maskDict, instanceNum, minCoords, maxCoords):
    # Check if line is contained in a different bounding box, need to check which instance is in front (below)
    # Don't check for the current instanceNum (key in boundingBoxDict)
    validForInstanceList = []
    # coords are row, col, ie (y,x)
    instanceLine = LineString([[minCoords[1], minCoords[0]], [maxCoords[1], maxCoords[0]]])
    for checkNumber, checkBoundingBox in boundingBoxDict.items():
        if checkNumber != instanceNum:
            checkPoly = Polygon(bboxToPoly(checkBoundingBox[0], checkBoundingBox[1], checkBoundingBox[2], checkBoundingBox[3]))
            # Check if line is contained in a different bounding box, need to check which instance is in front (below)
            lineInvalid = instanceLine.intersects(checkPoly)

            # May need to do more...something with checking average and deviation from average width of the 2 wires?
            if lineInvalid:
                imageBottom = maskDict[instanceNum].shape[0]
                instanceBottom = boundingBoxDict[instanceNum][3]
                checkInstanceBottom = checkBoundingBox[3]
                if abs(imageBottom - instanceBottom) < 20:
                    instanceTop = boundingBoxDict[instanceNum][1]
                    checkInstanceTop = checkBoundingBox[1]

                    # the box of interest is too close to the bottom
                    if instanceTop < checkInstanceTop:
                        # Good assumption that instance of interest is in front, since all wires ~same length and the top is lower than the check instance
                        validForInstanceList.append(True)
                    else:
                        # Maybe need to add more checks if too many lines are being eliminated
                        # intersectingMask = maskDict[checkNumber]
                        validForInstanceList.append(False)
                elif instanceBottom > checkInstanceBottom:
                    # the instance of interest is lower in the image, thus in front due to substrate tilt
                    validForInstanceList.append(True)
                else:
                    validForInstanceList.append(False)
            else:
                validForInstanceList.append(True)
    if all(validForInstanceList):
        return True
    # else:
    return False


def isEdgeInstance(mask, boundingBox, isVerticalSubSection):
    imageRight = mask.shape[1]
    imageBottom = mask.shape[0]
    if isVerticalSubSection:
        if boundingBox[0] < 20:
            # too close to left side
            return True
        elif abs(boundingBox[2] - imageRight) < 20:
            # too close to right side
            return True
    else:  # HorizontalSubSection
        if boundingBox[1] < 20:
            # too close to top side
            return True
        elif abs(boundingBox[3] - imageBottom) < 20:
            # too close to bottom side
            return True
    return False


def longestLineLengthInPolygon(maskPolygon, startCoordsRaw, endCoordsRaw):
    startCoord = Point(startCoordsRaw)
    endCoord = Point(endCoordsRaw)
    lineTest = LineString([startCoord, endCoord])
    testSegments = lineTest.intersection(maskPolygon)  # without .boundary, get the lines immediately

    LineLength = None
    if isinstance(testSegments, LineString):
        if not testSegments.is_empty:
            # The line is entirely inside the mask polygon
            LineLength = testSegments.length
    elif isinstance(testSegments, MultiLineString):
        # The line crosses the boundary of the mask polygon
        LineLength = 0
        for segment in testSegments:
            if segment.length > LineLength:
                LineLength = segment.length
    return LineLength


def getLinePoints(startCoordRaw, endCoordRaw):
    startCoord = Point(startCoordRaw)
    endCoord = Point(endCoordRaw)
    lineOfInterest = LineString([startCoord, endCoord])

    if sys.version_info < (3, 7):
        xyPoints = OrderedDict()
    else:
        # in Python 3.7 and newer dicts are ordered by default
        xyPoints = {}
    for pos in range(int(np.ceil(lineOfInterest.length)) + 1):
        interpolatedPoint = lineOfInterest.interpolate(pos).coords[0]
        roundedPoint = map(round, interpolatedPoint)
        xyPoints[tuple(roundedPoint)] = None
    # # Can iterated through via:
    # for key in xyPts.keys():
    #     print(key)
    return xyPoints


# @profile
def analyzeSingleInstance(maskDict, boundingBoxDict, instanceNumber, isVerticalSubSection):
    mask = maskDict[instanceNumber]
    boundingBox = boundingBoxDict[instanceNumber]
    # measCoords will be [row, col]
    measCoordsSet = set()
    pixelLengthList = []

    if showPlots:
        fig, ax = plt.subplots()
        ax.imshow(mask)
        plt.show(block=False)

    # maskCoords are [row,col] ie [y,x]
    subMask, subMaskCoords, maskAngle, rotatedNewMBB = centerXPercentofWire(mask, 0.7, isVerticalSubSection)
    if subMask is not None:
        if showPlots:
            fig2, ax2 = plt.subplots()
            ax2.imshow(subMask)
            plt.show(block=False)

        validLineSet = set()
        if not isEdgeInstance(mask, boundingBox, isVerticalSubSection):
            if isVerticalSubSection:
                subMaskCoordsDict = makeSubMaskCoordsDict(subMaskCoords, isVerticalSubSection)
                for line, linePixelsList in subMaskCoordsDict.items():
                    # Coords as row, col
                    minCoords = (line, min(linePixelsList))
                    maxCoords = (line, max(linePixelsList))
                    if isValidLine(boundingBoxDict, maskDict, instanceNumber, minCoords, maxCoords):
                        validLineSet.add(line)

                for line in validLineSet:
                    pixelLengthList.append(len(subMaskCoordsDict[line]))
                    for value in subMaskCoordsDict[line]:
                        measCoordsSet.add((line, value))

            else:
                # Coords are row, col ie (y,x)
                bottomLeftCoords = (rotatedNewMBB.exterior.coords.xy[1][0], rotatedNewMBB.exterior.coords.xy[0][0])
                bottomRightCoords = (rotatedNewMBB.exterior.coords.xy[1][1],rotatedNewMBB.exterior.coords.xy[0][1])
                topRightCoords = (rotatedNewMBB.exterior.coords.xy[1][2], rotatedNewMBB.exterior.coords.xy[0][2])
                topLeftCoords = (rotatedNewMBB.exterior.coords.xy[1][3], rotatedNewMBB.exterior.coords.xy[0][3])
                bottomLine = getLinePoints(bottomLeftCoords, bottomRightCoords)
                topLine = getLinePoints(topLeftCoords, topRightCoords)
                for bottomLinePoint, topLinePoint in zip(bottomLine.keys(), topLine.keys()):
                    if isValidLine(boundingBoxDict, maskDict, instanceNumber, bottomLinePoint, topLinePoint):
                        validLineSet.add((bottomLinePoint, topLinePoint))
                for line in validLineSet:
                    pixelLengthList.append(longestLineLengthInPolygon())

    return measCoordsSet, pixelLengthList, maskAngle, rotatedNewMBB


# @profile
def main():
    # outputsFileName = getFileOrDirList('file', 'Choose outputs pickle file')
    outputsFileName = '/home/mbraun/Downloads/outputmaskstest'
    outputs = fileHandling(outputsFileName)
    inputFileName = 'tiltedSEM/2020_02_06_MB0232_Reflectometry_002_cropped.jpg'
    if not path.isfile(inputFileName):
        quit()
    rawImage = Image.open(inputFileName)
    npImage = np.array(rawImage)

    if npImage.ndim < 3:
        if npImage.ndim == 2:
            # Assuming black and white image, just copy to all 3 color channels
            npImage = np.repeat(npImage[:, :, np.newaxis], 3, axis=2)
        else:
            print('The imported rawImage is 1 dimensional for some reason, check it out.')
            quit()

    boundingBoxDict = {}
    maskDict = {}
    numInstances = len(outputs['instances'])
    # Loop once to generate dict for checking each instance against all others
    for (mask, boundingBox, instanceNumber) in zip(outputs['instances'].pred_masks, outputs['instances'].pred_boxes, range(numInstances)):
        npMask = np.asarray(mask.cpu())

        # 0,0 at top left, and box is [left top right bottom] position ie [xmin ymin xmax ymax] (ie XYXY not XYWH)
        npBoundingBox = np.asarray(boundingBox.cpu())
        boundingBoxDict[instanceNumber] = npBoundingBox
        maskDict[instanceNumber] = npMask

    if parallelProcessing:
        with tqdm_joblib(tqdm(desc="Analyzing Instances", total=numInstances)) as progress_bar:
            analysisOutput = joblib.Parallel(n_jobs=multiprocessing.cpu_count())(
                joblib.delayed(analyzeSingleInstance)(maskDict, boundingBoxDict, instanceNumber, isVerticalSubSection) for
                instanceNumber in range(numInstances))
    else:
        analysisOutput = []
        for instanceNumber in range(numInstances):
            analysisOutput.append(analyzeSingleInstance(maskDict, boundingBoxDict, instanceNumber, isVerticalSubSection))
    quit()
    allMeasCoordsSetList = [entry[0] for entry in analysisOutput if entry[0] != set()]
    allMeasLengthList = [entry[1] for entry in analysisOutput if entry[0] != set()]
    allMeasAnglesList = [entry[2] for entry in analysisOutput if entry[0] != set()]
    allrbbList = [entry[3] for entry in analysisOutput if entry[0] != set()]
    allrbbAngleList = [entry['cardinal_angle_deg'] for entry in allrbbList]
    measMask = np.zeros(npImage.shape)[:, :, 0]
    numMeasInstances = len(allMeasCoordsSetList)
    for coordsSet, instanceNumber in zip(allMeasCoordsSetList, range(numMeasInstances)):
        for row, col in coordsSet:
            measMask[row][col] = (1 + instanceNumber) / numMeasInstances

    # https://stackoverflow.com/questions/17170229/setting-transparency-based-on-pixel-values-in-matplotlib
    measMask = np.ma.masked_where(measMask == 0, measMask)
    plt.subplots(figsize=(15, 12))
    plt.imshow(npImage, interpolation='none')
    plt.imshow(measMask, cmap=plt.get_cmap('plasma'), interpolation='none', alpha=0.5)
    plt.show()

    print('done')


if __name__ == "__main__":
    main()